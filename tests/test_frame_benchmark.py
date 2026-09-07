"""Check semantic boundaries and statistics, not timing-dependent assertions."""

import duckdb
import numpy as np
import pandas as pd
import pytest

from benchmarks.frame_benchmark import parse_args
from benchmarks.frame_statistics import compare, summarize
from benchmarks.frame_workloads import (
    WORKLOADS,
    fingerprint,
    pandas_transform,
    read_input,
    sql_query,
)


@pytest.fixture
def sales():
    return pd.DataFrame(
        {
            "id": [0, 1, 2, 3, 4],
            "account_id": [0, 0, 1, 2, 3],
            "region_id": [9, 10, 0, 49, 9],
            "amount_cents": [1000, 1000, 999, 101, 1234],
            "quantity": [1, 2, 3, 4, 2],
            "discount_pct": [np.nan, 0.0, 30.0, 15.0, 7.0],
        }
    )


@pytest.mark.parametrize("workload", WORKLOADS)
@pytest.mark.parametrize("empty", [False, True])
def test_engine_semantics(sales, workload, empty):
    accounts = pd.DataFrame({"account_id": [0, 1, 2, 3], "tier": [0, 1, 0, 1]})
    data = sales.iloc[:0] if empty else sales
    with duckdb.connect() as con:
        con.register("sales", data)
        con.register("accounts", accounts)
        actual = con.sql(sql_query(workload)).df()
    expected = pandas_transform(data, accounts, workload)
    columns = list(expected.columns)
    pd.testing.assert_frame_equal(
        actual.sort_values(columns).reset_index(drop=True),
        expected.sort_values(columns).reset_index(drop=True),
    )
    if not empty and workload == "clean_derive":
        assert expected.net_cents.tolist() == [1000, 2000, 2097, 343, 2295]
    if not empty and workload == "filter_project":
        assert expected.id.tolist() == [0, 4]
    if not empty and workload in ("groupby", "join_groupby"):
        assert expected.total_cents.sum() == 4334
        assert expected.row_count.sum() == 5


def test_parquet_pushdown_preserves_result(sales, tmp_path):
    path = tmp_path / "sales.parquet"
    sales.to_parquet(path, index=False)
    pushed = read_input(path, "filter_project", pushdown=True)
    full = read_input(path, "filter_project", pushdown=False)
    pd.testing.assert_frame_equal(
        pandas_transform(pushed, None, "filter_project"),
        pandas_transform(full, None, "filter_project"),
    )


def test_fingerprint_ignores_order_but_detects_value_change(sales):
    expected = fingerprint(sales)
    assert fingerprint(sales.iloc[::-1]) == expected
    changed = sales.copy()
    changed.loc[0, "amount_cents"] += 1
    assert fingerprint(changed) != expected


def test_statistics_sample_std_and_ratio():
    raw = pd.DataFrame(
        [
            {
                "rows": 100,
                "mode": "memory",
                "workload": "groupby",
                "engine": engine,
                "elapsed_ms": value * scale,
                "cpu_ms": value,
                "output_rows": 50,
                "block": index // 2,
            }
            for engine, scale in [("pandas", 2), ("duckdb", 1)]
            for index, value in enumerate([1, 2, 3, 4])
        ]
    )
    memory = raw[["rows", "mode", "workload", "engine"]].drop_duplicates()
    result = summarize(raw, memory).set_index("engine")
    assert result.loc["duckdb", "mean_ms"] == 2.5
    assert result.loc["duckdb", "std_ms"] == pytest.approx(np.sqrt(5 / 3))
    assert result.loc["duckdb", "blocks"] == 2
    comparison = compare(raw, 7).iloc[0]
    assert comparison.pandas_over_duckdb == 2
    assert comparison.ratio_ci95_low == 2
    assert comparison.ratio_ci95_high == 2


def test_invalid_repeat_configuration():
    with pytest.raises(SystemExit):
        parse_args(["--runs", "31"])
    with pytest.raises(SystemExit):
        parse_args(["--sizes", "100", "100"])
