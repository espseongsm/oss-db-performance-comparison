"""Compare local pandas/DuckDB and four Snowflake warehouse sizes by row count."""

import argparse
import hashlib
import importlib.metadata
import itertools
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone

import psutil

from benchmarks import sweep_snowflake
from benchmarks.frame_benchmark import ROOT, save_json, system_snapshot
from benchmarks.frame_snowflake import SnowflakeBackend
from benchmarks.frame_workloads import WORKLOADS
from benchmarks.sweep_data import prepare

PATHS = ("pandas", "duckdb", *sweep_snowflake.WAREHOUSES)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[100_000, 1_000_000, 10_000_000, 100_000_000, 1_000_000_000],
    )
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--blocks", type=int, default=6)
    parser.add_argument("--batch-rows", type=int, default=250_000)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--memory-gb", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--snowflake-connection", default="benchmark")
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args(argv)
    if args.pilot:
        args.sizes, args.runs, args.blocks = [1000, 10_000, 100_000], 2, 1
    if (
        min(
            *args.sizes,
            args.runs,
            args.blocks,
            args.batch_rows,
            args.threads,
            args.memory_gb,
            args.timeout_seconds,
        )
        <= 0
        or args.runs % args.blocks
        or args.seed < 0
        or len(set(args.sizes)) != len(args.sizes)
    ):
        parser.error(
            "Positive unique sizes/limits required; runs must be divisible by blocks."
        )
    if max(args.sizes) > 1_000_000_000:
        parser.error(
            "This validated checksum and capacity contract supports up to one billion rows."
        )
    args.sizes.sort()
    return args


def local_block(args, dataset, work, engine, runs, expected, folder, label):
    config = {
        "fact": dataset["fact"],
        "dimension": dataset["dimension"],
        "workload": work,
        "engine": engine,
        "runs": runs,
        "threads": args.threads,
        "memory_gb": args.memory_gb,
        "batch_rows": args.batch_rows,
        "expected": expected,
    }
    env = os.environ.copy()
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        env[name] = str(args.threads)
    log = folder / "workers" / f"{label}.jsonl"
    log.parent.mkdir(exist_ok=True)
    with log.open("w") as stdout, log.with_suffix(".stderr").open("w") as stderr:
        result = subprocess.run(
            [sys.executable, "-m", "benchmarks.sweep_worker", json.dumps(config)],
            cwd=ROOT,
            env=env,
            stdout=stdout,
            stderr=stderr,
            timeout=args.timeout_seconds * (runs + 1),
        )
    if result.returncode:
        raise RuntimeError(
            f"Local worker exited {result.returncode}: {log.with_suffix('.stderr').read_text()[-4000:]}"
        )
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[0]["type"] == "warmup" and len(records) == runs + 1
    return {"checksum": records[0]["checksum"], "measurements": records[1:]}


def main(argv=None):
    args = parse_args(argv)
    if shutil.disk_usage(ROOT).free < max(args.sizes) * 80:
        raise SystemExit(
            "Insufficient disk for canonical input, load parts and DuckDB spill allowance"
        )
    if psutil.virtual_memory().available < max(2**30, args.batch_rows * 256):
        raise SystemExit("Insufficient available RAM for bounded local batches")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = (
        ROOT / "results/warehouse-sweep" / f"{'pilot' if args.pilot else 'run'}-{stamp}"
    )
    folder.mkdir(parents=True)
    manifest = {
        "status": "preparing",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "settings": vars(args),
        "paths": PATHS,
        "metric_contract": {
            "pandas": "Local Parquet read, batched pandas transform and all output batches materialized; no warmup checksum time",
            "duckdb": "Local Parquet SQL, all output Arrow batches converted to pandas; no warmup checksum time",
            "snowflake": "QUERY_HISTORY total_elapsed_time: server compile, queues, execution and result generation; no full result download to Mac",
        },
        "validation_contract": "Full two-moment modular checksum in every block warmup; every measured run validates output row count. SQL checksum query and actual-query warmup are both excluded. No claim of per-run full checksum equality.",
        "source_sha256": {},
        "measurement_count": 0,
        "history_errors": [],
        "local_environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "cpu_logical": psutil.cpu_count(),
            "ram_bytes": psutil.virtual_memory().total,
            "versions": {
                p: importlib.metadata.version(p)
                for p in (
                    "pandas",
                    "duckdb",
                    "pyarrow",
                    "numpy",
                    "snowflake-connector-python",
                )
            },
        },
    }
    for path in [
        ROOT / "main.py",
        ROOT / "uv.lock",
        *sorted((ROOT / "benchmarks").glob("*.py")),
    ]:
        relative = path.relative_to(ROOT)
        target = folder / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        manifest["source_sha256"][str(relative)] = hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
    save_json(folder / "manifest.json", manifest)
    backend = None
    raw, validations, references, datasets = [], [], {}, {}
    try:
        for rows in args.sizes:
            print(f"공통 입력·적재용 파일 준비: {rows:,}행", flush=True)
            datasets[rows] = prepare(ROOT / "data/frame-benchmark", rows, args.seed)
            save_json(folder / "datasets.json", list(datasets.values()))
            for work in WORKLOADS:
                print(f"로컬 공통 결과 검증: {rows:,} {work}", flush=True)
                result = local_block(
                    args,
                    datasets[rows],
                    work,
                    "pandas",
                    0,
                    None,
                    folder,
                    f"reference-{rows}-{work}",
                )
                references[f"{rows}/{work}"] = result["checksum"]
                save_json(folder / "validation-reference.json", references)
        backend = SnowflakeBackend(
            args.snowflake_connection,
            args.timeout_seconds,
            use_keychain=True,
            warehouse="FRAME_BENCH_SMALL",
        )
        manifest["warehouses"] = sweep_snowflake.configure(backend)
        manifest["snowflake_session"] = backend.metadata
        save_json(folder / "manifest.json", manifest)
        sweep_snowflake.use(backend, "snowflake_small")
        for rows, dataset in datasets.items():
            print(f"공통 Snowflake 테이블 적재: {rows:,}행", flush=True)
            backend.load(dataset)
            save_json(folder / "snowflake-setup.json", backend.setup_records)
        sweep_snowflake.suspend(backend, "snowflake_small")
        manifest["status"] = "running"
        rng = random.Random(args.seed)
        cases = list(itertools.product(args.sizes, WORKLOADS))
        for block in range(1, args.blocks + 1):
            paths = list(PATHS)
            rng.shuffle(paths)
            for path in paths:
                rng.shuffle(cases)
                remote = path in sweep_snowflake.WAREHOUSES
                if remote:
                    sweep_snowflake.use(backend, path)
                for rows, work in cases:
                    case = dict(path=path, rows=rows, workload=work, block=block)
                    manifest["current_case"] = case
                    save_json(folder / "manifest.json", manifest)
                    print(
                        f"묶음 {block}/{args.blocks}: {rows:,} {work} {path}",
                        flush=True,
                    )
                    expected = references[f"{rows}/{work}"]
                    if remote:
                        result = sweep_snowflake.block(
                            backend,
                            path,
                            rows,
                            work,
                            args.runs // args.blocks,
                            expected,
                        )
                    else:
                        result = local_block(
                            args,
                            datasets[rows],
                            work,
                            path,
                            args.runs // args.blocks,
                            expected,
                            folder,
                            f"b{block}-{path}-{rows}-{work}",
                        )
                    validations.append(
                        {
                            **case,
                            **{k: v for k, v in result.items() if k != "measurements"},
                        }
                    )
                    save_json(folder / "block-validations.json", validations)
                    for index, sample in enumerate(result["measurements"], start=1):
                        record = {
                            **sample,
                            **case,
                            "run": (block - 1) * (args.runs // args.blocks) + index,
                            "metric_scope": "platform_query"
                            if remote
                            else "local_parquet_to_batches",
                        }
                        raw.append(record)
                        with (folder / "measurements.jsonl").open("a") as handle:
                            handle.write(json.dumps(record) + "\n")
                    manifest["measurement_count"] = len(raw)
                    manifest["history_errors"] = backend.history_errors
                    save_json(folder / "manifest.json", manifest)
                    with (folder / "host-snapshots.jsonl").open("a") as handle:
                        handle.write(json.dumps({**case, **system_snapshot()}) + "\n")
                if remote:
                    sweep_snowflake.suspend(backend, path)
        from benchmarks.sweep_report import render

        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        render(folder, raw, manifest, validations, references)
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
            for path in manifest.get("warehouses", {}):
                try:
                    sweep_snowflake.suspend(backend, path)
                except Exception as exc:
                    with (folder / "cleanup-errors.txt").open("a") as handle:
                        handle.write(str(exc) + "\n")
            backend.close()
    print(f"완료: {folder}", flush=True)
    return 0
