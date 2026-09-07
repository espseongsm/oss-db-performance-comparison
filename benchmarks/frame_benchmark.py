"""Generate shared Parquet data, validate and measure pandas versus DuckDB."""

from __future__ import annotations

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
from pathlib import Path

import pandas as pd
import psutil

from benchmarks.frame_statistics import compare, summarize
from benchmarks.frame_workloads import WORKLOADS, generate_dataset

ROOT = Path(__file__).resolve().parents[1]


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def system_snapshot() -> dict:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "system_cpu_pct": psutil.cpu_percent(interval=0.2),
        "load_average": list(os.getloadavg()),
        "available_memory_bytes": memory.available,
        "swap_used_bytes": swap.used,
        "swap_in_bytes": swap.sin,
        "swap_out_bytes": swap.sout,
    }


def run_worker(
    args: argparse.Namespace, case: dict, dataset: dict, profile: bool
) -> dict:
    env = os.environ.copy()
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        env[name] = str(args.threads)
    command = [
        sys.executable,
        "-m",
        "benchmarks.frame_worker",
        "--engine",
        case["engine"],
        "--mode",
        case["mode"],
        "--workload",
        case["workload"],
        "--fact",
        dataset["fact"],
        "--dimension",
        dataset["dimension"],
        "--runs",
        str(args.runs // args.blocks),
        "--threads",
        str(args.threads),
        "--memory-gb",
        str(args.memory_gb),
    ]
    if profile:
        command.append("--profile-memory")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=args.timeout_seconds,
    )
    if result.returncode:
        raise RuntimeError(f"Worker exit {result.returncode}: {result.stderr[-8000:]}")
    return json.loads(result.stdout)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes", nargs="+", type=int, default=[100_000, 1_000_000, 10_000_000]
    )
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--blocks", type=int, default=6)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--memory-gb", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/frame-benchmark")
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "results/pandas-duckdb"
    )
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args(argv)
    if args.pilot:
        args.sizes, args.runs, args.blocks = [1000, 10_000, 100_000], 2, 1
    positive = [
        *args.sizes,
        args.runs,
        args.blocks,
        args.threads,
        args.memory_gb,
        args.timeout_seconds,
    ]
    if min(positive) <= 0 or args.runs % args.blocks:
        parser.error("크기·횟수·제한은 양수이며 runs는 blocks의 배수여야 합니다.")
    if args.seed < 0:
        parser.error("seed는 0 이상이어야 합니다.")
    if len(set(args.sizes)) != len(args.sizes):
        parser.error("중복된 데이터 크기는 허용하지 않습니다.")
    args.sizes.sort()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.data_dir).free < sum(args.sizes) * 80:
        raise SystemExit("데이터 생성에 필요한 여유 디스크 공간이 부족합니다.")
    # Four-column inputs and merge intermediates can occupy several copies.
    if psutil.virtual_memory().available < max(args.sizes) * 256:
        raise SystemExit(
            "가장 큰 입력의 pandas 중간 결과를 위한 가용 메모리가 부족합니다."
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir / f"{'pilot' if args.pilot else 'run'}-{stamp}"
    output.mkdir(parents=True)
    settings = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
    }
    sources = [
        ROOT / "main.py",
        ROOT / "uv.lock",
        *sorted((ROOT / "benchmarks").glob("*.py")),
    ]
    manifest = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "cpu_logical_count": psutil.cpu_count(),
        "cpu_physical_count": psutil.cpu_count(logical=False),
        "physical_memory_bytes": psutil.virtual_memory().total,
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("pandas", "duckdb", "numpy", "pyarrow", "psutil", "scipy")
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
        "cache": "Warm filesystem cache; no OS cache purge; one warmup per block",
        "timing": "Execute to fully materialized pandas result, including DuckDB .df()",
        "excluded": "Imports, connection, memory-mode input preload/register, GC collection, validation",
        "memory": "Separate fresh-process single execution; OS peak RSS includes imports and preload; before validation",
        "uncertainty": "Mean: Student t over block means. Ratio: paired-block bootstrap, 10000 resamples. Exploratory, only 6 blocks by default.",
        "parallelism": "One worker at a time; DuckDB/Arrow/native library thread ceilings configured; pandas vector operations need not use all threads",
        "initial_system": system_snapshot(),
    }
    save_json(output / "manifest.json", manifest)
    raw, memory, references, order = [], [], {}, []

    def validate(case: dict, result: dict) -> None:
        key = f"{case['rows']}/{case['workload']}"
        actual = result["fingerprint"]
        if key in references and references[key] != actual:
            raise ValueError(f"Cross-engine/mode result mismatch: {case}")
        references[key] = actual

    try:
        datasets = {}
        for rows in args.sizes:
            print(f"데이터 준비: {rows:,}행", flush=True)
            datasets[rows] = generate_dataset(args.data_dir, rows, args.seed)
        save_json(output / "datasets.json", list(datasets.values()))
        rng = random.Random(args.seed)
        cases = list(itertools.product(args.sizes, ("parquet", "memory"), WORKLOADS))
        for block in range(1, args.blocks + 1):
            rng.shuffle(cases)
            for rows, mode, workload in cases:
                engines = ["pandas", "duckdb"]
                rng.shuffle(engines)
                for engine in engines:
                    case = {
                        "rows": rows,
                        "mode": mode,
                        "workload": workload,
                        "engine": engine,
                    }
                    print(
                        f"묶음 {block}/{args.blocks}: {rows:,} {mode} {workload} {engine}",
                        flush=True,
                    )
                    snapshot = system_snapshot()
                    order.append({**case, "block": block, **snapshot})
                    save_json(output / "execution-order.json", order)
                    if block == 1:
                        profile = run_worker(args, case, datasets[rows], True)
                        validate(case, profile)
                        memory.append(
                            {
                                **case,
                                **{
                                    k: v
                                    for k, v in profile.items()
                                    if k != "fingerprint"
                                },
                            }
                        )
                        pd.DataFrame(memory).to_csv(output / "memory.csv", index=False)
                    result = run_worker(args, case, datasets[rows], False)
                    validate(case, result)
                    for index, sample in enumerate(result["measurements"], start=1):
                        record = {
                            **case,
                            "block": block,
                            "run": (block - 1) * (args.runs // args.blocks) + index,
                            **sample,
                        }
                        raw.append(record)
                        with (output / "measurements.jsonl").open("a") as handle:
                            handle.write(json.dumps(record) + "\n")
                    save_json(output / "validation.json", references)
        raw_frame, memory_frame = pd.DataFrame(raw), pd.DataFrame(memory)
        counts = raw_frame.groupby(["rows", "mode", "workload", "engine"]).size()
        if len(counts) != len(cases) * 2 or not counts.eq(args.runs).all():
            raise ValueError("Measurement matrix is incomplete")
        raw_frame.to_csv(output / "measurements.csv", index=False)
        summary = summarize(raw_frame, memory_frame)
        comparison = compare(raw_frame, args.seed)
        summary.to_csv(output / "summary.csv", index=False)
        summary.to_json(output / "summary.json", orient="records", indent=2)
        comparison.to_csv(output / "comparison.csv", index=False)
        manifest.update(
            {
                "status": "complete",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "measurement_count": len(raw),
                "final_system": system_snapshot(),
            }
        )
        save_json(output / "manifest.json", manifest)
        from benchmarks.frame_charts import render_results

        render_results(output)
    except Exception:
        manifest.update(
            {
                "status": "failed",
                "error": traceback.format_exc(),
                "measurement_count": len(raw),
                "final_system": system_snapshot(),
            }
        )
        save_json(output / "manifest.json", manifest)
        raise
    print(f"완료: {output}", flush=True)
    return 0
