"""Functions embedded in the shared Snowflake Python stored procedure."""

import gc
import os
import platform
import sys
import time

import duckdb
import numpy as np
import pandas as pd
import psutil
import pyarrow as pa

from benchmarks.frame_snowflake import normalize_result, snowflake_query
from benchmarks.frame_workloads import (
    FILTERS,
    WORKLOADS,
    fingerprint,
    pandas_transform,
    sql_query,
)


def input_frame(session, table, columns, filtered=False):
    query = f"SELECT {', '.join(columns)} FROM {table}"
    if filtered:
        query += " WHERE " + " AND ".join(f"{c} {op} {v}" for c, op, v in FILTERS)
    frame = session.sql(query).to_pandas()
    frame.columns = [c.lower() for c in frame.columns]
    return frame.astype(
        {c: "float64" if c == "discount_pct" else "int64" for c in columns}
    )


def sample(session, config):
    engine, work = config["engine"], config["workload"]
    fact, dimension = config["fact"], config["dimension"]
    connection = None
    gc.collect()
    try:
        with session.query_history() as history:
            cpu_start, start = time.process_time_ns(), time.perf_counter_ns()
            if engine == "snowflake":
                loaded = prepared = start
                output = session.sql(snowflake_query(work, fact, dimension)).to_pandas()
            else:
                frame = input_frame(
                    session, fact, WORKLOADS[work], work == "filter_project"
                )
                accounts = (
                    input_frame(session, dimension, ["account_id", "tier"])
                    if work == "join_groupby"
                    else None
                )
                loaded = time.perf_counter_ns()
                if engine == "duckdb":
                    connection = duckdb.connect(
                        config={
                            "threads": config["threads"],
                            "memory_limit": f"{config['memory_gb']}GB",
                        }
                    )
                    connection.register("sales", frame)
                    if accounts is not None:
                        connection.register("accounts", accounts)
                    prepared = time.perf_counter_ns()
                    output = connection.sql(sql_query(work)).df()
                elif engine == "pandas":
                    prepared = loaded
                    output = pandas_transform(frame, accounts, work)
                else:
                    raise ValueError(f"Unknown engine: {engine}")
            output = normalize_result(output, [])
            finished, cpu_finished = time.perf_counter_ns(), time.process_time_ns()
        actual = fingerprint(output)
        if actual != config["expected"]:
            raise ValueError(f"Full-result fingerprint mismatch: {engine}/{work}")
        return {
            "elapsed_ms": (finished - start) / 1e6,
            "input_ms": (loaded - start) / 1e6 if engine != "snowflake" else None,
            "setup_ms": (prepared - loaded) / 1e6,
            "compute_to_dataframe_ms": (finished - prepared) / 1e6,
            "cpu_ms": (cpu_finished - cpu_start) / 1e6,
            "output_rows": len(output),
            "output_bytes": int(output.memory_usage(index=True, deep=True).sum()),
            "fingerprint": actual,
            "validated": True,
            "query_ids": [q.query_id for q in history.queries],
        }
    finally:
        if connection is not None:
            connection.close()


def runtime_info():
    import snowflake.snowpark

    return {
        "python": sys.version,
        "platform": platform.platform(),
        "visible_cpu_count": os.cpu_count(),
        "visible_memory_bytes": psutil.virtual_memory().total,
        "process_rss_bytes": psutil.Process().memory_info().rss,
        "versions": {
            "pandas": pd.__version__,
            "duckdb": duckdb.__version__,
            "numpy": np.__version__,
            "pyarrow": pa.__version__,
            "psutil": psutil.__version__,
            "snowflake-snowpark-python": snowflake.snowpark.__version__,
        },
        "resource_caveat": "Visible container resources do not establish equal per-engine CPU or RAM allocation.",
    }


def run(session, config):
    pa.set_cpu_count(config["threads"])
    pd.set_option("compute.use_numexpr", False)
    runtime = runtime_info()
    runtime["warehouse"] = session.get_current_warehouse()
    cache = session.sql("SHOW PARAMETERS LIKE 'USE_CACHED_RESULT' IN SESSION").collect()
    runtime["use_cached_result"] = cache[0]["value"]
    if runtime["use_cached_result"].lower() != "false":
        raise ValueError("Result cache must be disabled")
    sample(
        session, config
    )  # One unmeasured warmup, also checked against the reference.
    measurements = [sample(session, config) for _ in range(config["runs"])]
    return {"runtime": runtime, "measurements": measurements}
