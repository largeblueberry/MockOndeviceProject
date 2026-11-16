import cv2
import numpy as np
import pytest

from psafe.core.clock import FakeClock
from psafe.core.packets import Decision, Detection, FrameDetections, FramePacket, State
from psafe.outputs.viewer import ViewerOutput, annotate
from psafe.safety.roi import Roi
from psafe.sources.video import VideoFileSource

FPS, N = 10, 20


@pytest.fixture
def clip(tmp_path):
    """20프레임 MJPG 영상. i번 프레임의 밝기는 10*i라서 프레임을 구분할 수 있다."""
    path = str(tmp_path / "clip.avi")
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), FPS, (64, 48))
    assert w.isOpened()
    for i in range(N):
        w.write(np.full((48, 64, 3), 10 * i, np.uint8))
    w.release()
    return path


def level(pkt):
    return round(float(pkt.image.mean()) / 10)


def test_plays_every_frame_with_capture_timestamps(clip):
    src = VideoFileSource(FakeClock(), clip)
    src.open()
    frames = list(iter(src.read, None))
    src.close()
    assert [f.seq for f in frames] == list(range(N))
    assert [level(f) for f in frames] == list(range(N))
    assert frames[5].timestamp == pytest.approx(0.5)


def test_slow_consumer_gets_latest_frame(clip):
    clock = FakeClock()
    src = VideoFileSource(clock, clip)
    src.open()
    got = []
    for _ in range(4):
        f = src.read()
        got.append((f.seq, level(f)))
        clock.advance(0.25)                     # 250 ms "추론"
    assert got == [(0, 0), (2, 2), (5, 5), (7, 7)]   # 디코딩한 내용이 seq와 일치
    assert src.skipped == 4


def test_loop_keeps_seq_increasing(clip):
    src = VideoFileSource(FakeClock(), clip, loop=True)
    src.open()
    frames = [src.read() for _ in range(N + 5)]
    assert [f.seq for f in frames] == list(range(N + 5))
    assert [level(f) for f in frames[N:]] == [0, 1, 2, 3, 4]


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        VideoFileSource(FakeClock(), "nope.mp4").open()


def test_annotate_and_record(tmp_path):
    roi = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    img = np.full((120, 160, 3), 90, np.uint8)
    dets = FrameDetections(0, 0.0, (Detection("person", 0.9, (0.4, 0.3, 0.6, 0.8)),), "m", 12.0)
    dec = Decision(False, State.OCCUPIED, "person_in_roi", 0, 0.0, 0.0)

    out = annotate(img, Roi(roi), dets, dec)
    assert out.shape == img.shape and not np.array_equal(out, img)
    assert (img == 90).all()                                  # 입력은 변경되지 않음

    path = str(tmp_path / "rec.avi")
    v = ViewerOutput(roi, show=False, record_path=path)
    pkt = FramePacket(0, 0.0, "t", img)
    for _ in range(5):
        v.on_frame(pkt, dets, dec)
    v.close()
    cap = cv2.VideoCapture(path)
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 5
    cap.release()
