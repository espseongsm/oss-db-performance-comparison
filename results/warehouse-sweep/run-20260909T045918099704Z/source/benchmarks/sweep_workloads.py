"""Bounded-memory local execution and portable full-result checksums."""

import time

import duckdb
import numpy as np
import pandas as pd
import pyarrow.dataset as ds

from benchmarks.frame_workloads import WORKLOADS, pandas_transform, sql_query

PRIME = 2_147_483_647
OUTPUTS = {
    "filter_project": ["id", "region_id", "gross_cents"],
    "clean_derive": ["id", "net_cents"],
    "groupby": ["region_id", "total_cents", "total_quantity", "row_count"],
    "join_groupby": ["region_id", "tier", "total_cents", "row_count"],
}


def empty_checksum(work):
    return {"columns": OUTPUTS[work], "rows": 0, "sum_h": 0, "sum_h2": 0}


def add_checksum(total, frame):
    """Two order-independent modular moments, covering every output column/row."""
    assert list(frame.columns) == total["columns"]
    h = np.zeros(len(frame), dtype="int64")
    for column in frame:
        h = (h * 131 + frame[column].to_numpy(dtype="int64") % PRIME) % PRIME
    total["rows"] += len(frame)
    total["sum_h"] += int(h.sum())
    total["sum_h2"] += int(((h * h) % PRIME).sum())


def checksum_sql(query, work):
    h = "0"
    for column in OUTPUTS[work]:
        h = f"MOD(({h}) * 131 + MOD({column}, {PRIME}), {PRIME})"
    return (
        "SELECT COUNT(*) AS output_rows, CAST(COALESCE(SUM(h), 0) AS BIGINT) AS sum_h, "
        f"CAST(COALESCE(SUM(MOD(h * h, {PRIME})), 0) AS BIGINT) AS sum_h2 "
        f"FROM (SELECT {h} AS h FROM ({query}) AS output) AS hashed"
    )


def combine_aggregates(previous, current, work):
    if previous is None:
        return current
    keys = ["region_id"] if work == "groupby" else ["region_id", "tier"]
    return (
        pd.concat([previous, current], ignore_index=True)
        .groupby(keys, sort=False, as_index=False)
        .sum()[OUTPUTS[work]]
    )


def pandas_batches(config, timings):
    work = config["workload"]
    started = time.perf_counter_ns()
    accounts = pd.read_parquet(config["dimension"]) if work == "join_groupby" else None
    condition = (
        (ds.field("region_id") < 10) & (ds.field("amount_cents") >= 1000)
        if work == "filter_project"
        else None
    )
    scanner = ds.dataset(config["fact"], format="parquet").scanner(
        columns=WORKLOADS[work],
        filter=condition,
        batch_size=config["batch_rows"],
        batch_readahead=1,
        fragment_readahead=1,
        use_threads=True,
    )
    batches = iter(scanner.to_batches())
    timings["input_ms"] += (time.perf_counter_ns() - started) / 1e6
    aggregate = None
    while True:
        started = time.perf_counter_ns()
        batch = next(batches, None)
        if batch is None:
            timings["input_ms"] += (time.perf_counter_ns() - started) / 1e6
            break
        frame = batch.to_pandas()
        timings["input_ms"] += (time.perf_counter_ns() - started) / 1e6
        output = pandas_transform(frame, accounts, work)
        if work in ("groupby", "join_groupby"):
            aggregate = combine_aggregates(aggregate, output, work)
        else:
            yield output
    if work in ("groupby", "join_groupby"):
        yield (
            aggregate
            if aggregate is not None
            else pd.DataFrame({c: pd.Series(dtype="int64") for c in OUTPUTS[work]})
        )


def local_batches(config, timings, connection):
    if config["engine"] == "pandas":
        yield from pandas_batches(config, timings)
        return
    fact = config["fact"].replace("'", "''")
    dimension = config["dimension"].replace("'", "''")
    query = (
        sql_query(config["workload"])
        .replace("FROM sales", f"FROM read_parquet('{fact}')")
        .replace("JOIN accounts", f"JOIN read_parquet('{dimension}')")
    )
    for batch in connection.sql(query).to_arrow_reader(config["batch_rows"]):
        yield batch.to_pandas()


def consume_local(config, validate):
    timings = {"input_ms": 0.0}
    check = empty_checksum(config["workload"])
    rows = batches = output_bytes = 0
    connection = (
        duckdb.connect(
            config={
                "threads": config["threads"],
                "memory_limit": f"{config['memory_gb']}GB",
                "max_temp_directory_size": "64GB",
            }
        )
        if config["engine"] == "duckdb"
        else None
    )
    try:
        cpu, started = time.process_time_ns(), time.perf_counter_ns()
        for output in local_batches(config, timings, connection):
            output = output[OUTPUTS[config["workload"]]].astype("int64", copy=False)
            rows += len(output)
            batches += 1
            output_bytes += output.size * 8
            if validate:
                add_checksum(check, output)
        elapsed = (time.perf_counter_ns() - started) / 1e6
        cpu_ms = (time.process_time_ns() - cpu) / 1e6
    finally:
        if connection is not None:
            connection.close()
    return {
        "elapsed_ms": elapsed,
        "cpu_ms": cpu_ms,
        "input_ms": timings["input_ms"] if config["engine"] == "pandas" else None,
        "output_rows": rows,
        "output_bytes": output_bytes,
        "output_batches": batches,
        "checksum": check if validate else None,
    }
