"""Exercise credential isolation, persistence ordering and unattended reuse."""

import json
from types import SimpleNamespace

import pytest

from benchmarks import frame_benchmark, frame_keychain, frame_snowflake


@pytest.fixture
def authentication(monkeypatch):
    connector = pytest.importorskip("snowflake.connector")
    state = SimpleNamespace(
        stored=None, entered="private-test-value", connected=[], events=[],
        auth_error=False, store_error=None, closed=False,
    )

    def retrieve(service, user):
        state.events.append("read")
        return state.stored

    def persist(service, user, password):
        state.events.append("save")
        if state.store_error is not None:
            raise state.store_error
        state.stored = password

    class Cursor:
        description = [("name",), ("size",)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query):
            pass

        def fetchone(self):
            return ("ACCOUNT", "REGION", "ROLE", "WH", "DB", "PUBLIC", "test")

        def fetchall(self):
            return [("WH", "X-Small")]

    def close():
        state.closed = True

    def connect(**kwargs):
        state.events.append("connect")
        state.connected.append(kwargs)
        if state.auth_error:
            raise RuntimeError("Authentication rejected")
        return SimpleNamespace(cursor=Cursor, close=close)

    def prompt(message):
        state.events.append("prompt")
        return state.entered

    monkeypatch.setattr(frame_snowflake, "keychain_entry", lambda name: (
        SimpleNamespace(get_password=retrieve, set_password=persist), "test-service", "user"
    ))
    monkeypatch.setattr(connector, "connect", connect)
    monkeypatch.setattr(frame_snowflake.getpass, "getpass", prompt)
    monkeypatch.setattr(frame_snowflake.sys.stdin, "isatty", lambda: False)
    return state


def test_stored_password_works_without_terminal(authentication, capsys):
    state = authentication
    state.stored = "existing-test-value"
    backend = frame_snowflake.SnowflakeBackend("offline", 60, use_keychain=True)
    backend.close()
    assert state.events == ["read", "connect"]
    assert state.connected[0]["password"] == state.stored
    assert state.connected[0]["authenticator"] == "snowflake"
    assert state.connected[0]["client_store_temporary_credential"] is False
    assert state.connected[0]["client_request_mfa_token"] is False
    assert state.stored not in json.dumps(backend.metadata)
    captured = capsys.readouterr()
    assert state.stored not in captured.out + captured.err


def test_first_registration_requires_terminal(authentication):
    with pytest.raises(ValueError, match="interactive terminal"):
        frame_snowflake.SnowflakeBackend("offline", 60, use_keychain=True)
    assert authentication.events == ["read"]
    assert not authentication.connected


@pytest.mark.parametrize("auth_error", [False, True])
def test_password_saved_only_after_successful_login(authentication, monkeypatch, capsys, auth_error):
    state = authentication
    state.auth_error = auth_error
    monkeypatch.setattr(frame_snowflake.sys.stdin, "isatty", lambda: True)
    if auth_error:
        with pytest.raises(RuntimeError, match="Authentication rejected"):
            frame_snowflake.SnowflakeBackend("offline", 60, use_keychain=True)
        assert state.stored is None
        assert state.events == ["read", "prompt", "connect"]
    else:
        backend = frame_snowflake.SnowflakeBackend("offline", 60, use_keychain=True)
        backend.close()
        assert state.stored == state.entered
        assert state.events == ["read", "prompt", "connect", "save"]
    captured = capsys.readouterr()
    assert state.entered not in captured.out + captured.err


def test_rejected_replacement_preserves_existing_password(authentication, monkeypatch):
    state = authentication
    state.stored = "existing-test-value"
    state.auth_error = True
    monkeypatch.setattr(frame_snowflake.sys.stdin, "isatty", lambda: True)
    with pytest.raises(RuntimeError, match="Authentication rejected"):
        frame_snowflake.SnowflakeBackend(
            "offline", 60, use_keychain=True, prompt_password=True
        )
    assert state.stored == "existing-test-value"
    assert state.events == ["prompt", "connect"]


def test_empty_password_never_connects_or_saves(authentication, monkeypatch):
    state = authentication
    state.entered = ""
    monkeypatch.setattr(frame_snowflake.sys.stdin, "isatty", lambda: True)
    with pytest.raises(ValueError, match="must not be empty"):
        frame_snowflake.SnowflakeBackend("offline", 60, use_keychain=True)
    assert state.events == ["read", "prompt"]


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_keychain_write_failure_closes_connection(authentication, monkeypatch, error_type):
    state = authentication
    state.store_error = error_type("Keychain access denied")
    monkeypatch.setattr(frame_snowflake.sys.stdin, "isatty", lambda: True)
    with pytest.raises(error_type, match="Keychain access denied"):
        frame_snowflake.SnowflakeBackend("offline", 60, use_keychain=True)
    assert state.closed
    assert state.stored is None


def test_keychain_entries_are_scoped_to_account_and_user(monkeypatch):
    macos = pytest.importorskip("keyring.backends.macOS")
    config = pytest.importorskip("snowflake.connector.config_manager")
    profiles = {
        "first": {"account": "org-a", "user": "alice"},
        "other_account": {"account": "org-b", "user": "alice"},
        "other_user": {"account": "org-a", "user": "bob"},
        "alias": {"account": "org-a", "user": "alice"},
        "incomplete": {"account": "org-a"},
    }
    native_backend = object()
    monkeypatch.setattr(frame_keychain.sys, "platform", "darwin")
    monkeypatch.setattr(config, "CONFIG_MANAGER", {"connections": profiles})
    monkeypatch.setattr(macos, "Keyring", lambda: native_backend)
    first = frame_keychain.keychain_entry("first")
    assert first[0] is native_backend
    assert first == frame_keychain.keychain_entry("alias")
    assert first[1:] != frame_keychain.keychain_entry("other_account")[1:]
    assert first[1:] != frame_keychain.keychain_entry("other_user")[1:]
    with pytest.raises(ValueError, match="account and user"):
        frame_keychain.keychain_entry("incomplete")
    with pytest.raises(ValueError, match="Unknown Snowflake connection"):
        frame_keychain.keychain_entry("missing")


def test_non_macos_has_no_fallback(monkeypatch):
    monkeypatch.setattr(frame_keychain.sys, "platform", "linux")
    with pytest.raises(ValueError, match="requires macOS"):
        frame_keychain.keychain_entry("offline")


def test_interrupted_registration_marks_run_failed(monkeypatch, tmp_path):
    def interrupt(connection_name, timeout_seconds, **kwargs):
        assert kwargs == {"use_keychain": True}
        raise KeyboardInterrupt

    monkeypatch.setattr(frame_snowflake, "SnowflakeBackend", interrupt)
    with pytest.raises(KeyboardInterrupt):
        frame_benchmark.main([
            "--snowflake-connection", "offline", "--snowflake-keychain",
            "--sizes", "100", "--data-dir", str(tmp_path / "data"),
            "--output-dir", str(tmp_path / "output"),
        ])
    path = next((tmp_path / "output").glob("*/manifest.json"))
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["measurement_count"] == 0
    assert manifest["settings"]["snowflake_keychain"] is True
    assert "password" not in manifest["settings"]
