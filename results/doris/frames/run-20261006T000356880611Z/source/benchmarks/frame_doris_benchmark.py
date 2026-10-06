"""Measure Doris alone against the preserved pandas/DuckDB preprocessing inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import itertools
import json
import platform
import random
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from benchmarks.frame_doris import FACT_COLUMNS, DorisBackend
from benchmarks.frame_statistics import summarize
from benchmarks.frame_workloads import ACCOUNTS, DATA_VERSION, WORKLOADS

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "results/pandas-duckdb/run-20260907T022808832856Z"


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verified_datasets(
    data_dir: Path, reference_dir: Path, sizes: list[int], seed: int
) -> tuple[dict[int, dict], dict]:
    """Reject different inputs before connecting to Doris or collecting timings."""
    references = {
        item["rows"]: item
        for item in json.loads((reference_dir / "datasets.json").read_text())
    }
    fingerprints = json.loads((reference_dir / "validation.json").read_text())
    datasets = {}
    fact_schema = pa.schema(
        [
            (name, pa.float64() if name == "discount_pct" else pa.int64())
            for name in FACT_COLUMNS
        ]
    )
    dimension_schema = pa.schema([("account_id", pa.int64()), ("tier", pa.int64())])
    for rows in sizes:
        if rows not in references:
            raise ValueError(f"No historical dataset reference for {rows} rows")
        folder = data_dir / f"v{DATA_VERSION}-seed{seed}-rows{rows}"
        dataset = json.loads((folder / "dataset.json").read_text())
        reference = references[rows]
        expected = {
            "version": DATA_VERSION,
            "seed": seed,
            "rows": rows,
            "columns": len(FACT_COLUMNS),
            "dimension_rows": ACCOUNTS,
            "compression": "snappy",
            "row_group_size": 100_000,
        }
        for key, value in expected.items():
            if dataset.get(key) != value or reference.get(key) != value:
                raise ValueError(f"Dataset/reference setting mismatch: {rows}/{key}")
        for name, schema, count in (
            ("sales.parquet", fact_schema, rows),
            ("accounts.parquet", dimension_schema, ACCOUNTS),
        ):
            path = folder / name
            digest = file_hash(path)
            if digest != reference["sha256"][name] or digest != dataset["sha256"][name]:
                raise ValueError(f"Historical input checksum mismatch: {path}")
            parquet = pq.ParquetFile(path)
            if not parquet.schema_arrow.equals(schema, check_metadata=False):
                raise ValueError(f"Historical input schema mismatch: {path}")
            if parquet.metadata.num_rows != count:
                raise ValueError(f"Historical input row count mismatch: {path}")
        for workload in WORKLOADS:
            if f"{rows}/{workload}" not in fingerprints:
                raise ValueError(f"Missing historical result: {rows}/{workload}")
        dataset["fact"] = str((folder / "sales.parquet").resolve())
        dataset["dimension"] = str((folder / "accounts.parquet").resolve())
        datasets[rows] = dataset
    return datasets, fingerprints


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
    parser.add_argument("--reference-dir", type=Path, default=REFERENCE)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "results/doris/frames"
    )
    args = parser.parse_args(argv)
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
    ):
        parser.error("Data sizes, repetitions and resource limits must be positive")
    if (
        args.runs % args.blocks
        or len(set(args.sizes)) != len(args.sizes)
        or args.seed < 0
    ):
        parser.error(
            "Runs must divide into blocks; sizes must be unique and seed nonnegative"
        )
    args.sizes.sort()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir / f"run-{stamp}"
    output.mkdir(parents=True)
    manifest = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "platform": platform.platform(),
        "python": sys.version,
        "engine": "doris",
        "mode": "warehouse",
        "reference_dir": str(args.reference_dir.resolve()),
        "cache": "Warm cache; one untimed verified query before each block",
        "timing": "SQL request to fully fetched/materialized/normalized int64 pandas result",
        "excluded": "Connection, schema, Stream Load, warmup, explicit GC and fingerprint verification",
        "memory": "Server memory is not client RSS; reported cpu_ms is client CPU only",
        "comparison_scope": "Preserved historical local measurements have a different input/deployment path",
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("pandas", "numpy", "pyarrow", "scipy")
        },
        "measurement_count": 0,
    }
    raw, memory, order, validation = [], [], [], {}
    backend = None
    try:
        datasets, fingerprints = verified_datasets(
            args.data_dir, args.reference_dir, args.sizes, args.seed
        )
        manifest["reference_sha256"] = {
            name: file_hash(args.reference_dir / name)
            for name in ("datasets.json", "validation.json", "manifest.json")
        }
        sources = (
            Path(__file__),
            ROOT / "benchmarks/frame_doris.py",
            ROOT / "benchmarks/frame_workloads.py",
            ROOT / "benchmarks/frame_statistics.py",
            ROOT / "scripts/doris_client.py",
            ROOT / "main.py",
            ROOT / "uv.lock",
        )
        manifest["source_sha256"] = {}
        for source in sources:
            relative = source.relative_to(ROOT)
            manifest["source_sha256"][str(relative)] = file_hash(source)
            target = output / "source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        save_json(output / "manifest.json", manifest)
        save_json(output / "datasets.json", list(datasets.values()))
        backend = DorisBackend(
            f"frames_doris_{stamp.lower()}",
            threads=args.threads,
            memory_gb=args.memory_gb,
            timeout_seconds=args.timeout_seconds,
        )
        manifest["doris"] = backend.metadata
        save_json(output / "manifest.json", manifest)
        for dataset in datasets.values():
            print(f"Doris loading {dataset['rows']:,} historical rows", flush=True)
            backend.load(dataset)
            save_json(output / "doris-setup.json", backend.setup_records)
        cases = list(itertools.product(args.sizes, WORKLOADS))
        rng = random.Random(args.seed)
        with (output / "measurements.jsonl").open("w") as handle:
            for block in range(1, args.blocks + 1):
                rng.shuffle(cases)
                for rows, workload in cases:
                    case = {
                        "rows": rows,
                        "mode": "warehouse",
                        "workload": workload,
                        "engine": "doris",
                    }
                    expected = fingerprints[f"{rows}/{workload}"]
                    order.append({**case, "block": block})
                    save_json(output / "execution-order.json", order)
                    print(
                        f"Block {block}/{args.blocks}: {rows:,} {workload} doris",
                        flush=True,
                    )
                    if block == 1:
                        profile = backend.run(case, 0, expected, profile=True)
                        memory.append(
                            {
                                **case,
                                **{
                                    key: value
                                    for key, value in profile.items()
                                    if key != "fingerprint"
                                },
                            }
                        )
                        pd.DataFrame(memory).to_csv(output / "memory.csv", index=False)
                    result = backend.run(case, args.runs // args.blocks, expected)
                    validation[f"{rows}/{workload}"] = result["fingerprint"]
                    for index, sample in enumerate(result["measurements"], start=1):
                        record = {
                            **case,
                            "block": block,
                            "run": (block - 1) * (args.runs // args.blocks) + index,
                            **sample,
                        }
                        raw.append(record)
                        handle.write(json.dumps(record) + "\n")
                    handle.flush()
                    save_json(output / "validation.json", validation)
        raw_frame = pd.DataFrame(raw)
        counts = raw_frame.groupby(["rows", "mode", "workload", "engine"]).size()
        if (
            len(counts) != len(args.sizes) * len(WORKLOADS)
            or not counts.eq(args.runs).all()
        ):
            raise ValueError("Doris measurement matrix is incomplete")
        raw_frame.to_csv(output / "measurements.csv", index=False)
        summary = summarize(raw_frame, pd.DataFrame(memory))
        summary.to_csv(output / "summary.csv", index=False)
        summary.to_json(output / "summary.json", orient="records", indent=2)
        manifest["status"] = "complete"
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    except (Exception, KeyboardInterrupt):
        manifest["status"] = "failed"
        manifest["error"] = traceback.format_exc()
        raise
    finally:
        manifest["measurement_count"] = len(raw)
        if backend is not None:
            try:
                backend.close()
                manifest["cleanup"] = "complete"
            except Exception:
                manifest["cleanup"] = "failed"
                manifest["cleanup_error"] = traceback.format_exc()
                manifest["status"] = "failed"
                save_json(output / "manifest.json", manifest)
                raise
        save_json(output / "manifest.json", manifest)
    print(f"Complete: {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
