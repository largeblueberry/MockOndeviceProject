"""모델 manifest: 모델을 만든 쪽(PC/Colab)과 실행하는 쪽(Pi) 사이의 유일한 계약 (DISCUSSION.md 합의 3).

클래스 이름, 입력 크기, 출력 좌표 단위처럼 **모델 파일의 성질**은 config가 아니라 모델 옆
`manifest.json`에 둔다. 모델을 바꾸면서 config를 안 고쳐도 틀린 값이 조용히 들어가지 않는다.
config에는 배치 위치마다 달라지는 값(conf_threshold, class_map, classes, threads)만 남긴다.

형식 (schema 1). 한 폴더에 컴파일 전/후 파일을 같이 두므로 파일별 해시를 가진다:

    {
      "schema": 1,
      "id": "y8n_320_incar_v1",
      "imgsz": 320,
      "names": ["person"],
      "box_units": "normalized",          # 출력 박스 좌표: "pixel"(0..imgsz) | "normalized"(0..1)
      "output_layout": "chw",             # "chw" = (1, 4+nc, N) | "hwc" = (1, N, 4+nc)
      "files": {
        "model_edgetpu.tflite": {"sha256": "...", "edgetpu": true},
        "model_int8.tflite":    {"sha256": "...", "edgetpu": false}
      },
      "build":   {"tensorflow": "2.17.1", "edgetpu_compiler": "16.0"},   # 선택
      "compile": {"tpu_ops": 120, "total_ops": 125, "first_cpu_op": null} # 선택
      // 그 밖의 키(eval, calibration, git_commit ...)는 기록용. 읽기만 하고 검사하지 않는다.
    }
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 1
BOX_UNITS = ("pixel", "normalized")
OUTPUT_LAYOUTS = ("chw", "hwc")
EDGETPU_MARKER = b"edgetpu-custom-op"   # 컴파일된 모델의 flatbuffer에 custom op 이름으로 들어 있다


class ManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileEntry:
    sha256: str
    edgetpu: bool | None = None


@dataclass(frozen=True, slots=True)
class ModelManifest:
    path: Path
    id: str
    imgsz: int
    names: tuple[str, ...]
    box_units: str
    output_layout: str
    files: dict[str, FileEntry]
    build_tensorflow: str | None = None
    tpu_ops: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> "ModelManifest":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"model manifest not found: {path} (every .tflite model needs one, "
                                    "see docs/edge_tpu/DISCUSSION.md 합의 3)")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ManifestError(f"{path}: invalid JSON: {e}") from e
        return cls.from_dict(raw, path)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: Path) -> "ModelManifest":
        def need(key: str, typ: type | tuple[type, ...]) -> Any:
            if key not in raw:
                raise ManifestError(f"{path}: missing '{key}'")
            v = raw[key]
            if not isinstance(v, typ) or isinstance(v, bool) and typ is int:
                raise ManifestError(f"{path}: '{key}' has wrong type {type(v).__name__}")
            return v

        if need("schema", int) != SCHEMA:
            raise ManifestError(f"{path}: schema {raw['schema']} not supported (expected {SCHEMA})")
        imgsz = need("imgsz", int)
        if imgsz <= 0:
            raise ManifestError(f"{path}: imgsz must be positive")
        names = need("names", list)
        if not names or not all(isinstance(n, str) and n for n in names):
            raise ManifestError(f"{path}: 'names' must be a non-empty list of strings")
        box_units = need("box_units", str)
        if box_units not in BOX_UNITS:
            raise ManifestError(f"{path}: box_units must be one of {BOX_UNITS}")
        layout = need("output_layout", str)
        if layout not in OUTPUT_LAYOUTS:
            raise ManifestError(f"{path}: output_layout must be one of {OUTPUT_LAYOUTS}")

        files: dict[str, FileEntry] = {}
        for fname, entry in need("files", dict).items():
            if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
                raise ManifestError(f"{path}: files['{fname}'] needs a 'sha256' string")
            tpu = entry.get("edgetpu")
            if tpu is not None and not isinstance(tpu, bool):
                raise ManifestError(f"{path}: files['{fname}'].edgetpu must be true/false")
            files[fname] = FileEntry(entry["sha256"].lower(), tpu)
        if not files:
            raise ManifestError(f"{path}: 'files' is empty")

        build = raw.get("build") or {}
        compile_ = raw.get("compile") or {}
        tpu_ops = compile_.get("tpu_ops")
        if tpu_ops is not None and (not isinstance(tpu_ops, int) or isinstance(tpu_ops, bool)):
            raise ManifestError(f"{path}: compile.tpu_ops must be an integer")
        tf_ver = build.get("tensorflow")
        return cls(path, str(raw.get("id", path.parent.name)), imgsz, tuple(n.replace(" ", "_") for n in names),
                   box_units, layout, files, str(tf_ver) if tf_ver is not None else None, tpu_ops, raw)


def default_manifest_path(weights: str | Path) -> Path:
    return Path(weights).with_name("manifest.json")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_edgetpu_compiled(data: bytes) -> bool:
    """private API(_get_ops_details) 대신 파일 바이트로 판단한다. 런타임 버전과 무관하고 HW 없이 동작한다.
    ⚠️ 첫 실제 컴파일 결과물로 확인할 것 (DISCUSSION.md R4-4)."""
    return EDGETPU_MARKER in data


def major_minor(version: str) -> tuple[int, int] | None:
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None
