"""Offline semantic/runner tests; these are not live Snowflake validation."""

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from benchmarks import frame_benchmark, frame_snowflake
from benchmarks.frame_snowflake import normalize_result, snowflake_query
from benchmarks.frame_workloads import (
    WORKLOADS,
    fingerprint,
    make_batch,
    pandas_transform,
)


@pytest.mark.parametrize("workload", WORKLOADS)
@pytest.mark.parametrize("empty", [False, True])
def test_portable_snowflake_expressions(workload, empty):
    frame = make_batch(0, 500, np.random.default_rng(42))
    if empty:
        frame = frame.iloc[:0]
    accounts = pd.DataFrame(
        {"account_id": range(100_000), "tier": np.arange(100_000) % 4}
    )
    expected = pandas_transform(frame, accounts, workload)
    with duckdb.connect() as connection:
        connection.register("sf_fact", frame)
        connection.register("sf_accounts", accounts)
        actual = connection.sql(
            snowflake_query(workload, "sf_fact", "sf_accounts")
        ).df()
    columns = list(expected.columns)
    pd.testing.assert_frame_equal(
        actual.sort_values(columns).reset_index(drop=True),
        expected.sort_values(columns).reset_index(drop=True),
    )


def test_connector_integer_normalization():
    result = normalize_result(
        pd.DataFrame({"ID": np.array([1], dtype="int8"), "TOTAL_CENTS": [2**53 + 1]}),
        [],
    )
    assert result.columns.tolist() == ["id", "total_cents"]
    assert result.total_cents.iloc[0] == 2**53 + 1
    assert all(dtype == "int64" for dtype in result.dtypes)
    empty = normalize_result(None, ["ID", "TOTAL_CENTS"])
    assert empty.empty and empty.columns.tolist() == ["id", "total_cents"]
    assert all(dtype == "int64" for dtype in empty.dtypes)


@pytest.mark.parametrize("fail", [False, True])
def test_optional_backend_matrix_and_cleanup(monkeypatch, tmp_path, fail):
    output_frame = pd.DataFrame(
        {
            "region_id": [0],
            "total_cents": [123],
            "total_quantity": [2],
            "row_count": [100],
        }
    )
    expected = fingerprint(output_frame)
    profile = {"fingerprint": expected, "output_bytes": 164}
    instances = []

    def fake_worker(args, case, dataset, is_profile):
        if is_profile:
            return profile
        return {
            "fingerprint": expected,
            "measurements": [
                {
                    "elapsed_ms": float(index + 1),
                    "cpu_ms": 1.0,
                    "output_rows": 1,
                    "validated": True,
                }
                for index in range(args.runs // args.blocks)
            ],
        }

    class OfflineBackend:
        def __init__(self, connection_name, timeout_seconds):
            self.closed = False
            self.metadata = {"offline_test_only": True}
            self.history_errors, self.setup_records = [], []
            instances.append(self)

        def load(self, dataset):
            self.setup_records.append({"rows": dataset["rows"]})

        def run(self, case, runs, is_profile):
            if fail:
                raise ValueError("Injected remote validation failure")
            if is_profile:
                return profile
            return {
                "fingerprint": expected,
                "measurements": [
                    {
                        "elapsed_ms": 10.0 + index,
                        "cpu_ms": 1.0,
                        "output_rows": 1,
                        "validated": True,
                        "query_id": f"offline-{index}",
                        "server_execution_ms": None,
                    }
                    for index in range(runs)
                ],
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(frame_snowflake, "SnowflakeBackend", OfflineBackend)
    monkeypatch.setattr(frame_benchmark, "run_worker", fake_worker)
    monkeypatch.setattr(frame_benchmark, "WORKLOADS", {"groupby": []})
    # Exercise aggregation and output routing without emitting mock chart artifacts.
    from benchmarks import frame_charts

    monkeypatch.setattr(frame_charts, "render_results", lambda folder: None)
    argv = [
        "--snowflake-connection",
        "offline",
        "--sizes",
        "100",
        "--runs",
        "2",
        "--blocks",
        "1",
        "--data-dir",
        str(tmp_path / "data"),
        "--output-dir",
        str(tmp_path / "results"),
    ]
    if fail:
        with pytest.raises(ValueError, match="Injected remote"):
            frame_benchmark.main(argv)
    else:
        assert frame_benchmark.main(argv) == 0
    assert instances[0].closed
    folder = next((tmp_path / "results").iterdir())
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["status"] == ("failed" if fail else "complete")
    if not fail:
        raw = pd.read_csv(folder / "measurements.csv")
        assert len(raw) == 10
        assert raw.groupby(["engine", "mode"]).size().to_dict() == {
            ("pandas", "memory"): 2,
            ("pandas", "parquet"): 2,
            ("duckdb", "memory"): 2,
            ("duckdb", "parquet"): 2,
            ("snowflake", "warehouse"): 2,
        }
        assert pd.read_csv(folder / "comparison.csv")["mode"].tolist() == [
            "memory",
            "parquet",
        ]
        assert (
            not raw.loc[raw.engine == "snowflake", "server_execution_ms"].notna().any()
        )


def test_example_contains_no_credentials():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads(
        (root / "config/snowflake-connections.example.toml").read_text()
    )
    assert config["benchmark"]["authenticator"] == "externalbrowser"
    assert "password" not in config["benchmark"]


@pytest.mark.parametrize("option", ["--snowflake-password-prompt", "--snowflake-keychain"])
def test_password_prompt_requires_connection(option):
    with pytest.raises(SystemExit):
        frame_benchmark.parse_args([option])


@pytest.mark.parametrize("interactive", [False, True])
def test_password_prompt_is_terminal_only(monkeypatch, capsys, interactive):
    connector = pytest.importorskip("snowflake.connector")
    calls = []

    def fake_connect(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("Stop before network connection")

    monkeypatch.setattr(connector, "connect", fake_connect)
    monkeypatch.setattr(frame_snowflake.sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr(frame_snowflake.getpass, "getpass", lambda prompt: "test-secret")
    expected = RuntimeError if interactive else ValueError
    with pytest.raises(expected):
        frame_snowflake.SnowflakeBackend("offline", 60, prompt_password=True)
    assert len(calls) == int(interactive)
    if interactive:
        assert calls[0]["password"] == "test-secret"
        assert calls[0]["authenticator"] == "snowflake"
        assert calls[0]["client_store_temporary_credential"] is False
        assert calls[0]["client_request_mfa_token"] is False
    captured = capsys.readouterr()
    assert "test-secret" not in captured.out + captured.err
