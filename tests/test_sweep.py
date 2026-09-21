"""Cross-batch semantics, portable SQL checksum and six-path completeness checks."""

import itertools
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest

from benchmarks.frame_snowflake import snowflake_query
from benchmarks.frame_workloads import (
    WORKLOADS,
    make_batch,
    pandas_transform,
    sql_query,
)
from benchmarks.sweep_benchmark import PATHS
from benchmarks.sweep_report import audit
from benchmarks.sweep_snowflake import block
from benchmarks.sweep_workloads import (
    add_checksum,
    checksum_sql,
    consume_local,
    empty_checksum,
)


@pytest.mark.parametrize(
    "work,engine,empty",
    list(itertools.product(WORKLOADS, ("pandas", "duckdb"), (False, True))),
)
def test_all_local_batches_match_full_frame(tmp_path, work, engine, empty):
    frame = make_batch(0, 500, np.random.default_rng(12))
    if empty:
        frame = frame.iloc[:0]
    accounts = pd.DataFrame(
        {"account_id": range(100_000), "tier": np.arange(100_000) % 4}
    )
    fact, dim = tmp_path / "sales.parquet", tmp_path / "accounts.parquet"
    frame.to_parquet(fact, row_group_size=41, index=False)
    accounts.to_parquet(dim, index=False)
    config = dict(
        engine=engine,
        workload=work,
        fact=str(fact),
        dimension=str(dim),
        threads=1,
        memory_gb=1,
        batch_rows=17,
    )
    expected = empty_checksum(work)
    output = pandas_transform(frame, accounts, work)
    add_checksum(expected, output)
    result = consume_local(config, True)
    assert result["checksum"] == expected
    measured = consume_local(config, False)
    assert measured["checksum"] is None and measured["output_rows"] == len(output)


@pytest.mark.parametrize(
    "work,empty", list(itertools.product(WORKLOADS, (False, True)))
)
def test_sql_checksum_matches_pandas(work, empty):
    frame = make_batch(0, 500, np.random.default_rng(31))
    if empty:
        frame = frame.iloc[:0]
    accounts = pd.DataFrame(
        {"account_id": range(100_000), "tier": np.arange(100_000) % 4}
    )
    expected = empty_checksum(work)
    add_checksum(expected, pandas_transform(frame, accounts, work))
    with duckdb.connect() as connection:
        connection.register("sales", frame)
        connection.register("accounts", accounts)
        for query in (sql_query(work), snowflake_query(work, "sales", "accounts")):
            result = connection.sql(checksum_sql(query, work)).fetchone()
            assert result == (expected["rows"], expected["sum_h"], expected["sum_h2"])


def test_checksum_is_partition_independent_and_integer_exact():
    frame = pd.DataFrame(
        {"id": [0, 2**53 + 1, 999_999_999], "net_cents": [2_147_483_646, 1, 2**53 + 2]}
    )
    full, split = empty_checksum("clean_derive"), empty_checksum("clean_derive")
    add_checksum(full, frame)
    for index in (2, 0, 1):
        add_checksum(split, frame.iloc[index : index + 1])
    assert split == full
    with duckdb.connect() as connection:
        connection.register("result", frame)
        values = connection.sql(
            checksum_sql("SELECT * FROM result", "clean_derive")
        ).fetchone()
    assert values == (full["rows"], full["sum_h"], full["sum_h2"])


def test_snowflake_uses_platform_time_without_downloading_result():
    class Cursor:
        sfqid = "query"
        rowcount = 4

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query):
            pass

        def fetchone(self):
            return (4, 10, 20)

    def history(samples):
        for sample in samples:
            sample.update(
                server_total_ms=37, server_execution_ms=23, server_compilation_ms=14
            )

    backend = SimpleNamespace(
        connection=SimpleNamespace(cursor=Cursor),
        tables={100: "sales"},
        dimension="accounts",
        _add_query_history=history,
    )
    expected = dict(columns=["id", "net_cents"], rows=4, sum_h=10, sum_h2=20)
    result = block(backend, "snowflake_small", 100, "clean_derive", 2, expected)
    assert all(r["elapsed_ms"] == 37 for r in result["measurements"])
    assert result["checksum"] == expected


def sweep_evidence():
    references = {
        f"1000/{work}": dict(columns=[], rows=4, sum_h=10, sum_h2=20)
        for work in WORKLOADS
    }
    records, checks = [], []
    for work, path in itertools.product(WORKLOADS, PATHS):
        checks.append(
            dict(
                rows=1000,
                workload=work,
                path=path,
                block=1,
                checksum=references[f"1000/{work}"],
            )
        )
        for run in (1, 2):
            records.append(
                dict(
                    rows=1000,
                    workload=work,
                    path=path,
                    run=run,
                    block=1,
                    output_rows=4,
                    elapsed_ms=float(run),
                    server_total_ms=float(run)
                    if path.startswith("snowflake")
                    else None,
                    query_id=f"{work}/{path}/{run}",
                    metric_scope="platform_query"
                    if path.startswith("snowflake")
                    else "local_parquet_to_batches",
                )
            )
    return (
        pd.DataFrame(records),
        {"settings": dict(sizes=[1000], runs=2, blocks=1)},
        checks,
        references,
    )


def test_six_path_summary():
    summary = audit(*sweep_evidence())
    assert len(summary) == 24 and summary.mean_ms.eq(1.5).all()
    assert summary.std_ms.tolist() == pytest.approx([2**-0.5] * 24)


@pytest.mark.parametrize(
    "fault", ["condition", "duplicate", "scope", "checksum", "run"]
)
def test_six_path_audit_rejects_bad_evidence(fault):
    raw, manifest, checks, references = sweep_evidence()
    if fault == "condition":
        raw = raw.loc[raw.path != "pandas"]
    elif fault == "duplicate":
        raw = pd.concat([raw, raw.iloc[:1]])
    elif fault == "scope":
        raw.loc[raw.path.eq("snowflake_small"), "metric_scope"] = "client_roundtrip"
    elif fault == "checksum":
        checks[0]["checksum"] = {"rows": 99}
    else:
        raw = raw.iloc[1:]
    with pytest.raises(ValueError):
        audit(raw, manifest, checks, references)
