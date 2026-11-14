import pytest

from psafe.core.config import ConfigError, load_config, parse_config

MIN = {"source": {"name": "dummy"}, "model": {"name": "dummy"}}


def test_default_config_loads():
    cfg = load_config("config/default.yaml")
    assert cfg.source.name == "dummy"
    assert cfg.fsm.clear_time_s == 3.0
    assert cfg.outputs[0].name == "console"


def test_defaults_fill_missing_sections():
    cfg = parse_config(MIN)
    assert cfg.runner.mode == "sync"
    assert cfg.fsm.clear_min_frames == 10


def test_typo_is_an_error():
    with pytest.raises(ConfigError, match="clear_tme_s"):
        parse_config({**MIN, "fsm": {"clear_tme_s": 1.0}})


def test_missing_required_section():
    with pytest.raises(ConfigError):
        parse_config({"source": {"name": "dummy"}})


def test_bad_runner_mode():
    with pytest.raises(ConfigError, match="mode"):
        parse_config({**MIN, "runner": {"mode": "async"}})
