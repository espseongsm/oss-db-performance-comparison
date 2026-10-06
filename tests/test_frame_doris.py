import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest

from benchmarks.frame_doris import DorisBackend, doris_query, normalize_result
from benchmarks.frame_doris_benchmark import verified_datasets
from benchmarks.frame_workloads import (
    WORKLOADS,
    fingerprint,
    generate_dataset,
    pandas_transform,
)


@pytest.mark.parametrize("workload", WORKLOADS)
def test_doris_sql_matches_integer_null_and_join_semantics(workload):
    sales = pd.DataFrame(
        {
            "id": [0, 1, 2, 3, 4],
            "account_id": [0, 1, 0, 2, 1],
            "region_id": [0, 0, 10, 9, 9],
            "amount_cents": [1001, 999, 100_000, 0, 1001],
            "quantity": [3, 10, 10, 1, 7],
            "discount_pct": [30.0, np.nan, 1.0, 0.0, 9.0],
        }
    )
    accounts = pd.DataFrame({"account_id": [0, 1, 2], "tier": [0, 1, 2]})
    with duckdb.connect() as db:
        db.register("fixture_sales", sales)
        db.register("fixture_accounts", accounts)
        result = db.sql(doris_query(workload, "fixture_sales", "fixture_accounts"))
        actual = normalize_result(result.fetchall(), result.columns)
    expected = pandas_transform(sales, accounts, workload)
    assert fingerprint(actual) == fingerprint(expected)


def test_result_normalization_handles_decimal_and_empty_integer_columns():
    frame = normalize_result([(Decimal("12"), 5)], ["TOTAL_CENTS", "ROW_COUNT"])
    assert frame.to_dict("list") == {"total_cents": [12], "row_count": [5]}
    assert frame.dtypes.tolist() == [np.dtype("int64"), np.dtype("int64")]
    empty = normalize_result([], ["ID", "NET_CENTS"])
    assert list(empty.columns) == ["id", "net_cents"]
    assert empty.dtypes.tolist() == [np.dtype("int64"), np.dtype("int64")]
    with pytest.raises((TypeError, ValueError)):
        normalize_result([(None,)], ["id"])


def test_timing_includes_fetch_and_dataframe_normalization(monkeypatch):
    import benchmarks.frame_doris as module

    events = []
    cursor = SimpleNamespace(
        description=[("ID",), ("NET_CENTS",)],
        fetchall=lambda: events.append("fetch") or [(0, Decimal("7"))],
        close=lambda: events.append("close"),
    )
    backend = DorisBackend.__new__(DorisBackend)
    backend.db = SimpleNamespace(
        execute=lambda query: events.append("execute") or cursor
    )
    normalize = module.normalize_result

    def materialize(rows, columns):
        events.append("normalize")
        return normalize(rows, columns)

    wall = iter([0, 1_000_000, 9_000_000])
    cpu = iter([0, 2_000_000])
    monkeypatch.setattr(module, "normalize_result", materialize)
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: next(wall))
    monkeypatch.setattr(module.time, "process_time_ns", lambda: next(cpu))
    output, sample = backend._execute("SELECT id, net_cents")
    assert events == ["execute", "fetch", "normalize", "close"]
    assert output.net_cents.tolist() == [7]
    assert sample["elapsed_ms"] == 9
    assert sample["execute_roundtrip_ms"] == 1
    assert sample["fetch_dataframe_ms"] == 8


@pytest.fixture
def reference_inputs(tmp_path):
    data = tmp_path / "data"
    reference = tmp_path / "reference"
    reference.mkdir()
    dataset = generate_dataset(data, 12, 20260907)
    (reference / "datasets.json").write_text(json.dumps([dataset]))
    (reference / "validation.json").write_text(
        json.dumps({f"12/{workload}": {} for workload in WORKLOADS})
    )
    return data, reference, dataset


def test_reference_matching_checks_file_hash_and_deterministic_settings(
    reference_inputs,
):
    data, reference, dataset = reference_inputs
    verified, _ = verified_datasets(data, reference, [12], 20260907)
    assert verified[12]["sha256"] == dataset["sha256"]
    fact = Path(dataset["fact"])
    with fact.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        verified_datasets(data, reference, [12], 20260907)


def test_reference_matching_rejects_seed_and_missing_result(reference_inputs):
    data, reference, dataset = reference_inputs
    dataset["seed"] = 1
    (reference / "datasets.json").write_text(json.dumps([dataset]))
    with pytest.raises(ValueError, match="setting mismatch"):
        verified_datasets(data, reference, [12], 20260907)
    dataset["seed"] = 20260907
    (reference / "datasets.json").write_text(json.dumps([dataset]))
    (reference / "validation.json").write_text("{}")
    with pytest.raises(ValueError, match="Missing historical result"):
        verified_datasets(data, reference, [12], 20260907)


def test_historical_result_mismatch_prevents_timed_repetitions():
    backend = DorisBackend.__new__(DorisBackend)
    backend.tables = {1: "sales_1"}
    backend.dimension = "accounts"
    calls = []
    output = pd.DataFrame({"id": [0], "net_cents": [1]})

    def execute(query):
        calls.append(query)
        return output, {"elapsed_ms": 1}

    backend._execute = execute
    different = fingerprint(pd.DataFrame({"id": [0], "net_cents": [2]}))
    with pytest.raises(ValueError, match="historical reference"):
        backend.run({"rows": 1, "workload": "clean_derive"}, 30, different)
    assert len(calls) == 1


def test_changed_repeated_result_is_not_returned_as_successful_measurement():
    backend = DorisBackend.__new__(DorisBackend)
    backend.tables = {1: "sales_1"}
    backend.dimension = "accounts"
    correct = pd.DataFrame({"id": [0], "net_cents": [1]})
    wrong = pd.DataFrame({"id": [0], "net_cents": [2]})
    responses = iter([correct, wrong])
    backend._execute = lambda query: (next(responses), {"elapsed_ms": 1})
    with pytest.raises(ValueError, match="repeated result"):
        backend.run({"rows": 1, "workload": "clean_derive"}, 1, fingerprint(correct))
