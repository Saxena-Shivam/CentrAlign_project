from centralign_agent.config import _env_bool


def test_env_bool_accepts_trailing_semicolon(monkeypatch):
    monkeypatch.setenv("HEADLESS", "false;")
    assert _env_bool("HEADLESS", True) is False


def test_env_bool_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("HEADLESS", "sometimes")
    try:
        _env_bool("HEADLESS")
    except ValueError as exc:
        assert "HEADLESS" in str(exc)
    else:
        raise AssertionError("Invalid boolean values must remain errors.")
