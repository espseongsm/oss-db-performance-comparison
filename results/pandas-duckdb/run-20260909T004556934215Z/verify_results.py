"""Offline audit of this completed run; never connects to Snowflake."""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

FOLDER = Path(__file__).resolve().parent
ROOT = FOLDER.parents[2]
KEYS = ("rows", "mode", "workload", "engine")


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from benchmarks.frame_workloads import (
        WORKLOADS,
        fingerprint,
        pandas_transform,
        read_input,
    )

    manifest = json.loads((FOLDER / "manifest.json").read_text())
    records = [json.loads(line) for line in (FOLDER / "measurements.jsonl").open()]
    validation = json.loads((FOLDER / "validation.json").read_text())
    datasets = json.loads((FOLDER / "datasets.json").read_text())
    setup = json.loads((FOLDER / "snowflake-setup.json").read_text())
    order = json.loads((FOLDER / "execution-order.json").read_text())
    assert manifest["status"] == "complete" and not manifest["settings"]["pilot"]
    assert manifest["measurement_count"] == len(records) == 1800
    paths = [(m, e) for m in ("memory", "parquet") for e in ("pandas", "duckdb")]
    paths.append(("warehouse", "snowflake"))
    expected = {
        (rows, mode, work, engine, run)
        for rows, work, (mode, engine), run in itertools.product(
            (100_000, 1_000_000, 10_000_000), WORKLOADS, paths, range(1, 31)
        )
    }
    actual = [tuple(r[k] for k in KEYS) + (r["run"],) for r in records]
    assert len(set(actual)) == len(actual) and set(actual) == expected
    groups = defaultdict(list)
    for record in records:
        assert record["validated"] is True
        assert record["block"] == (record["run"] - 1) // 5 + 1
        assert math.isfinite(record["elapsed_ms"]) and record["elapsed_ms"] > 0
        groups[tuple(record[k] for k in KEYS)].append(record["elapsed_ms"])
        ref = validation[f"{record['rows']}/{record['workload']}"]
        assert record["output_rows"] == ref["rows"]
    assert len(groups) == 60 and all(len(g) == 30 for g in groups.values())
    assert Counter(tuple(r[k] for k in KEYS) for r in order) == {
        key: 6 for key in groups
    }
    assert len({(*[r[k] for k in KEYS], r["block"]) for r in order}) == 360
    raw_frame = pd.DataFrame(records)
    csv_frame = pd.read_csv(FOLDER / "measurements.csv")
    pd.testing.assert_frame_equal(
        raw_frame, csv_frame, check_dtype=False, rtol=1e-12, atol=1e-9
    )
    summary = list(csv.DictReader((FOLDER / "summary.csv").open()))
    assert len(summary) == 60
    recomputed = {}
    max_errors = defaultdict(float)
    for row in summary:
        key = (int(row["rows"]), row["mode"], row["workload"], row["engine"])
        values = groups[key]
        metrics = {
            "mean_ms": statistics.mean(values),
            "std_ms": statistics.stdev(values),
            "median_ms": statistics.median(values),
            "min_ms": min(values),
            "max_ms": max(values),
        }
        assert int(row["n"]) == 30 and int(row["blocks"]) == 6
        for name, value in metrics.items():
            max_errors[name] = max(max_errors[name], abs(value - float(row[name])))
            assert math.isclose(value, float(row[name]), rel_tol=1e-12, abs_tol=1e-9)
        recomputed[key] = metrics
    assert len(recomputed) == 60
    comparison = list(csv.DictReader((FOLDER / "comparison.csv").open()))
    assert len(comparison) == 24
    for row in comparison:
        key = (int(row["rows"]), row["mode"], row["workload"])
        ratio = recomputed[(*key, "pandas")]["mean_ms"] / recomputed[
            (*key, "duckdb")
        ]["mean_ms"]
        assert math.isclose(ratio, float(row["pandas_over_duckdb"]), rel_tol=1e-12)
    remote = [r for r in records if r["engine"] == "snowflake"]
    assert len(remote) == len({r["query_id"] for r in remote}) == 360
    server_fields = [k for k in remote[0] if k.startswith("server_")]
    assert not manifest["snowflake_history_errors"]
    for row in remote:
        assert all(row[f] is not None and row[f] >= 0 for f in server_fields)
        assert math.isclose(
            row["elapsed_ms"],
            row["execute_roundtrip_ms"] + row["fetch_dataframe_ms"],
            abs_tol=1e-9,
        )
    components = pd.read_csv(FOLDER / "snowflake-components.csv", header=[0, 1], index_col=[0, 1])
    component_checks = 0
    for key, group in raw_frame[raw_frame.engine == "snowflake"].groupby(["rows", "workload"]):
        for field in components.columns.get_level_values(0).unique():
            values = group[field].tolist()
            for metric, value in {
                "count": len(values), "mean": statistics.mean(values),
                "std": statistics.stdev(values), "median": statistics.median(values),
                "min": min(values), "max": max(values),
            }.items():
                assert math.isclose(value, components.loc[key, (field, metric)], rel_tol=1e-12, abs_tol=1e-8)
                component_checks += 1
    source_matches = []
    for name, digest in manifest["source_sha256"].items():
        source = FOLDER / "source/measured" / name
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest, name
        source_matches.append(name)
    input_checks = []
    fingerprints = []
    for dataset in datasets:
        for key in ("fact", "dimension"):
            path = Path(dataset[key])
            with path.open("rb") as handle:
                assert hashlib.file_digest(handle, "sha256").hexdigest() == dataset["sha256"][path.name]
            input_checks.append(str(path.relative_to(ROOT)))
        accounts = pd.read_parquet(dataset["dimension"])
        for workload in WORKLOADS:
            frame = read_input(Path(dataset["fact"]), workload, pushdown=True)
            output = pandas_transform(frame, accounts, workload)
            key = f"{dataset['rows']}/{workload}"
            assert fingerprint(output) == validation[key], key
            fingerprints.append(key)
            del frame, output
    assert set(fingerprints) == set(validation) and len(fingerprints) == 12
    for item in setup:
        assert all(r[1] == "LOADED" and r[3] == item["rows"] and r[5] == 0 for r in item["copy_result"])
    assert len(setup) == 4
    qa = {
        "status": "passed",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "run": FOLDER.name,
        "measurements": len(records), "groups": len(groups),
        "runs_per_group": 30, "blocks_per_group": 6, "samples_per_block": 5,
        "engine_counts": dict(Counter(r["engine"] for r in records)),
        "duplicates": 0, "missing_combinations": 0, "all_validated": True,
        "raw_csv_matches_jsonl": True,
        "statistics": {"method": "Python statistics, sample stdev (ddof=1)", "max_absolute_error_ms": dict(max_errors)},
        "local_mean_ratios_checked": len(comparison),
        "snowflake": {"unique_queries": 360, "history_errors": [], "missing_server_values": 0, "component_statistics_checked": component_checks, "client_time_components_sum": True, "setup_tables_verified": 4},
        "source_hashes_matched": source_matches,
        "input_hashes_matched": input_checks,
        "fresh_local_fingerprints_matched": fingerprints,
        "validation_limit": "Remote result bodies and per-run fingerprints are not persisted. Their equality is supported by runtime validation, recorded flags, and unchanged source hashes. Fresh offline pandas results match all 12 saved references; Snowflake queries were not rerun.",
        "interpretation": "Share with caveats: different local/cloud paths and resources, six blocks, warm caches, external system activity, no causal network attribution or server-memory comparison.",
    }
    (FOLDER / "qa.json").write_text(json.dumps(qa, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "passed", "measurements": 1800, "groups": 60, "fingerprints": len(fingerprints), "component_checks": component_checks, "max_error_ms": dict(max_errors)}))


if __name__ == "__main__":
    main()
