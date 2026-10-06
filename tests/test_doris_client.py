"""Regression checks for FE accepting connections before BE storage is ready."""

import pytest

from scripts.doris_client import DorisClient


def test_waits_for_live_backend_with_reported_storage(monkeypatch):
    client = DorisClient(None, "benchmark")
    states = iter([
        [],
        [{"Alive": "false", "AvailCapacity": "100.000 GB"}],
        [{"Alive": "true", "AvailCapacity": "0.000 "}],
        [{"Alive": "true", "AvailCapacity": "100.000 GB"}],
    ])
    sleeps = []
    monkeypatch.setattr(client, "backends", lambda: next(states))
    monkeypatch.setattr("scripts.doris_client.time.sleep", sleeps.append)
    client.wait_ready()
    assert sleeps == [1, 1, 1]


def test_backend_timeout_prevents_setup(monkeypatch):
    client = DorisClient(None, "benchmark")
    monkeypatch.setattr(client, "backends", lambda: [])
    monkeypatch.setattr("scripts.doris_client.time.sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="available storage"):
        client.wait_ready(attempts=2)
