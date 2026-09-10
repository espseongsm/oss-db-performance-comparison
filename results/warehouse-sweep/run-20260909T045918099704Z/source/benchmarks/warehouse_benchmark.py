"""Compare three paths inside the same existing Small Snowflake warehouse."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import itertools
import json
import random
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from benchmarks import warehouse_handler
from benchmarks.frame_benchmark import ROOT, save_json
from benchmarks.frame_snowflake import (
    HISTORY_COLUMNS,
    SnowflakeBackend,
    normalize_result,
    snowflake_query,
)
from benchmarks.frame_workloads import (
    FILTERS,
    WORKLOADS,
    fingerprint,
    generate_dataset,
    pandas_transform,
    read_input,
    sql_query,
)

PACKAGES = {
    "snowflake-snowpark-python": "1.54.0",
    "pandas": "2.3.3",
    "duckdb": "1.5.5",
    "pyarrow": "23.0.1",
    "numpy": "2.5.3",
    "psutil": "7.2.2",
}
ENGINES = ("pandas", "duckdb", "snowflake")


def procedure_source():
    imports = "import gc, os, platform, sys, time\nimport duckdb, numpy as np, pandas as pd, psutil, pyarrow as pa\n"
    constants = f"WORKLOADS = {WORKLOADS!r}\nFILTERS = {FILTERS!r}\n"
    functions = [
        pandas_transform,
        sql_query,
        fingerprint,
        snowflake_query,
        normalize_result,
        warehouse_handler.input_frame,
        warehouse_handler.sample,
        warehouse_handler.runtime_info,
        warehouse_handler.run,
    ]
    return imports + constants + "\n\n".join(inspect.getsource(f) for f in functions)


def collect_history(connection, ids):
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT QUERY_ID, "
            + ",".join(HISTORY_COLUMNS)
            + " FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT=>1000)) "
            + f"WHERE QUERY_ID IN ({placeholders})",
            ids,
        )
        return {
            row[0]: dict(zip(HISTORY_COLUMNS.values(), row[1:], strict=True))
            for row in cursor.fetchall()
        }


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snowflake-connection", default="benchmark")
    parser.add_argument("--warehouse", default="FRAME_BENCH_SMALL")
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[100_000, 1_000_000, 10_000_000]
    )
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--blocks", type=int, default=6)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--memory-gb", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args(argv)
    if args.pilot:
        args.sizes, args.runs, args.blocks = [1000, 10_000, 100_000], 2, 1
    if (
        min(
            *args.sizes,
            args.runs,
            args.blocks,
            args.threads,
            args.memory_gb,
            args.timeout_seconds,
        )
        <= 0
        or args.runs % args.blocks
        or len(set(args.sizes)) != len(args.sizes)
        or args.seed < 0
    ):
        parser.error(
            "Positive unique sizes and limits required; runs must be divisible by blocks."
        )
    return args


def main(argv=None):
    args = parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = (
        ROOT / "results/snowflake-small" / f"{'pilot' if args.pilot else 'run'}-{stamp}"
    )
    folder.mkdir(parents=True)
    source = procedure_source()
    (folder / "procedure.py").write_text(source)
    manifest = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "settings": vars(args),
        "packages": PACKAGES,
        "timing": "Same Snowflake tables to a complete pandas DataFrame inside the Python stored procedure; Mac receives metrics only. SQL scan/compute is fused; input_ms is null for native SQL.",
        "excluded": "Connection, upload/COPY, procedure startup and CALL transport, warmup, GC, fingerprints, query-history lookup, connection cleanup.",
        "measurement_count": 0,
        "history_errors": [],
    }
    sources = [
        ROOT / "main.py",
        ROOT / "uv.lock",
        *sorted((ROOT / "benchmarks").glob("*.py")),
    ]
    manifest["source_sha256"] = {}
    for path in sources:
        relative = path.relative_to(ROOT)
        target = folder / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        manifest["source_sha256"][str(relative)] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    save_json(folder / "manifest.json", manifest)
    backend = None
    raw, references, calls, datasets = [], {}, [], {}
    try:
        backend = SnowflakeBackend(
            args.snowflake_connection,
            args.timeout_seconds,
            use_keychain=True,
            warehouse=args.warehouse,
        )
        manifest["snowflake"] = backend.metadata
        if backend.metadata["warehouse_settings"]["size"].lower() != "small":
            raise ValueError("This comparison requires a Small warehouse")
        package_sql = ",".join(
            f"'{name}=={version}'" for name, version in PACKAGES.items()
        )
        procedure = backend.prefix + "_RUN"
        with backend.connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TEMPORARY PROCEDURE {procedure}(CONFIG VARIANT) RETURNS VARIANT LANGUAGE PYTHON RUNTIME_VERSION='3.12' PACKAGES=({package_sql}) HANDLER='run' EXECUTE AS CALLER AS $$"
                + source
                + "$$"
            )
        for rows in args.sizes:
            print(f"공통 입력·Small 적재: {rows:,}행", flush=True)
            dataset = generate_dataset(ROOT / "data/frame-benchmark", rows, args.seed)
            datasets[rows] = dataset
            backend.load(dataset)
            accounts = pd.read_parquet(dataset["dimension"])
            for work in WORKLOADS:
                frame = read_input(Path(dataset["fact"]), work, pushdown=True)
                output = pandas_transform(frame, accounts, work)
                references[f"{rows}/{work}"] = fingerprint(output)
                del frame, output
            save_json(folder / "snowflake-setup.json", backend.setup_records)
        save_json(folder / "datasets.json", list(datasets.values()))
        save_json(folder / "validation.json", references)
        save_json(folder / "manifest.json", manifest)
        cases = list(itertools.product(args.sizes, WORKLOADS, ENGINES))
        rng = random.Random(args.seed)
        for block in range(1, args.blocks + 1):
            rng.shuffle(cases)
            for rows, work, engine in cases:
                print(
                    f"Small 묶음 {block}/{args.blocks}: {rows:,} {work} {engine}",
                    flush=True,
                )
                config = {
                    "fact": backend.tables[rows],
                    "dimension": backend.dimension,
                    "engine": engine,
                    "workload": work,
                    "runs": args.runs // args.blocks,
                    "threads": args.threads,
                    "memory_gb": args.memory_gb,
                    "expected": references[f"{rows}/{work}"],
                }
                started = time.perf_counter()
                with backend.connection.cursor() as cursor:
                    cursor.execute(
                        f"CALL {procedure}(PARSE_JSON(%s))", (json.dumps(config),)
                    )
                    result = json.loads(cursor.fetchone()[0])
                    call_id = cursor.sfqid
                call_ms = (time.perf_counter() - started) * 1000
                assert result["runtime"]["warehouse"].strip('"') == args.warehouse
                if result["runtime"]["versions"] != PACKAGES:
                    raise ValueError(
                        "Stored-procedure runtime versions differ from pinned packages"
                    )
                calls.append(
                    {
                        "rows": rows,
                        "workload": work,
                        "engine": engine,
                        "block": block,
                        "call_id": call_id,
                        "call_ms": call_ms,
                        "runtime": result["runtime"],
                    }
                )
                save_json(folder / "calls.json", calls)
                ids = [
                    q for sample in result["measurements"] for q in sample["query_ids"]
                ]
                try:
                    history = collect_history(backend.connection, ids)
                except Exception as exc:
                    history = {}
                    manifest["history_errors"].append(str(exc))
                for index, sample in enumerate(result["measurements"], start=1):
                    if (
                        sample["fingerprint"] != config["expected"]
                        or not sample["validated"]
                    ):
                        raise ValueError("Returned sample did not validate")
                    record = {
                        "rows": rows,
                        "mode": "warehouse",
                        "workload": work,
                        "engine": engine,
                        "block": block,
                        "run": (block - 1) * config["runs"] + index,
                        "call_id": call_id,
                        **sample,
                    }
                    record["server_queries"] = {
                        q: history.get(q) for q in sample["query_ids"]
                    }
                    raw.append(record)
                    with (folder / "measurements.jsonl").open("a") as handle:
                        handle.write(json.dumps(record) + "\n")
                manifest["measurement_count"] = len(raw)
                save_json(folder / "manifest.json", manifest)
        from benchmarks.warehouse_report import render_results

        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        render_results(folder, pd.DataFrame(raw), manifest)
        manifest["status"] = "complete"
        save_json(folder / "manifest.json", manifest)
    except (Exception, KeyboardInterrupt):
        manifest.update(
            status="failed", error=traceback.format_exc(), measurement_count=len(raw)
        )
        save_json(folder / "manifest.json", manifest)
        raise
    finally:
        if backend is not None:
            backend.close()
    print(f"완료: {folder}", flush=True)
    return 0
