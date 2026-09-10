"""One isolated process for one engine, size, mode and measurement block."""

from __future__ import annotations

import argparse
import gc
import json
import resource
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
import psutil
import pyarrow as pa

from benchmarks.frame_workloads import (
    WORKLOADS,
    fingerprint,
    pandas_transform,
    read_input,
    sql_query,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("pandas", "duckdb"), required=True)
    parser.add_argument("--mode", choices=("parquet", "memory"), required=True)
    parser.add_argument("--workload", choices=WORKLOADS, required=True)
    parser.add_argument("--fact", type=Path, required=True)
    parser.add_argument("--dimension", type=Path, required=True)
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--memory-gb", type=int, required=True)
    parser.add_argument("--profile-memory", action="store_true")
    args = parser.parse_args()
    pa.set_cpu_count(args.threads)
    pa.set_io_thread_count(args.threads)
    pd.set_option("compute.use_numexpr", False)
    process = psutil.Process()
    import_rss = process.memory_info().rss
    frame = accounts = connection = None
    if args.mode == "memory":
        frame = read_input(args.fact, args.workload, pushdown=False)
        if args.workload == "join_groupby":
            accounts = pd.read_parquet(args.dimension, engine="pyarrow")
    query = sql_query(args.workload)
    if args.engine == "duckdb":
        connection = duckdb.connect(
            config={
                "threads": args.threads,
                "memory_limit": f"{args.memory_gb}GB",
            }
        )
        if args.mode == "memory":
            connection.register("sales", frame)
            if accounts is not None:
                connection.register("accounts", accounts)
        else:
            fact = str(args.fact).replace("'", "''")
            dim = str(args.dimension).replace("'", "''")
            query = query.replace("FROM sales", f"FROM read_parquet('{fact}')")
            query = query.replace("JOIN accounts", f"JOIN read_parquet('{dim}')")

    def execute() -> pd.DataFrame:
        if connection is not None:
            return connection.sql(query).df()
        data, dimension = frame, accounts
        if args.mode == "parquet":
            data = read_input(args.fact, args.workload, pushdown=True)
            if args.workload == "join_groupby":
                dimension = pd.read_parquet(args.dimension, engine="pyarrow")
        return pandas_transform(data, dimension, args.workload)

    setup_rss = process.memory_info().rss
    if args.profile_memory:
        output = execute()
        # Read the OS high-water mark before hashing/validation allocates memory.
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024)
        result = {
            "import_rss_bytes": import_rss,
            "setup_rss_bytes": setup_rss,
            "process_peak_rss_bytes": peak_bytes,
            "output_bytes": int(output.memory_usage(index=True, deep=True).sum()),
            "fingerprint": fingerprint(output),
        }
    else:
        output = execute()  # One untimed warmup per fresh block process.
        expected = fingerprint(output)
        del output
        measurements = []
        for _ in range(args.runs):
            gc.collect()  # Excluded; automatic GC stays enabled inside execute().
            cpu_start = time.process_time_ns()
            start = time.perf_counter_ns()
            output = execute()
            elapsed_ms = (time.perf_counter_ns() - start) / 1e6
            cpu_ms = (time.process_time_ns() - cpu_start) / 1e6
            actual = fingerprint(output)
            if actual != expected:
                raise ValueError("Repeated result does not match warmup")
            measurements.append(
                {
                    "elapsed_ms": elapsed_ms,
                    "cpu_ms": cpu_ms,
                    "output_rows": len(output),
                    "validated": True,
                }
            )
            del output
        result = {"measurements": measurements, "fingerprint": expected}
    if connection is not None:
        connection.close()
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
