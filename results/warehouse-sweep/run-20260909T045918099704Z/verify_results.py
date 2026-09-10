"""Independent, read-only benchmark data audit; writes only independent-audit.json.

Run from project root with: uv run --no-sync python <path-to-this-file>
Uses only the standard library; imports no benchmark/production summarization code.
"""

import csv
import itertools
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIZES = [100_000, 1_000_000, 10_000_000, 100_000_000, 1_000_000_000]
TASKS = ["filter_project", "clean_derive", "groupby", "join_groupby"]
PATHS = [
    "pandas",
    "duckdb",
    "snowflake_xsmall",
    "snowflake_small",
    "snowflake_medium",
    "snowflake_large",
]
COMPONENTS = [
    "server_total_ms",
    "server_execution_ms",
    "server_compilation_ms",
    "server_provisioning_queue_ms",
    "server_overload_queue_ms",
    "client_execute_roundtrip_ms",
]
KEYS = ["rows", "workload", "path"]
issues = []
checks = Counter()


def check(condition, label, detail=""):
    checks[label] += 1
    if not condition:
        issues.append({"check": label, "detail": str(detail)})


def read_json(name):
    return json.loads((ROOT / name).read_text())


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_csv(name):
    with (ROOT / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


def key(record):
    return int(record["rows"]), record["workload"], record["path"]


def finite_nonnegative(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def csv_equal(actual, expected):
    # Decimal prevents converting integers through binary floats, including >2**53.
    if expected is None:
        return actual in ("", None)
    if isinstance(expected, int):
        return actual not in ("", None) and Decimal(actual) == Decimal(expected)
    if isinstance(expected, float):
        return actual not in ("", None) and float(actual) == expected
    if isinstance(expected, (dict, list)):
        return json.loads(actual) == expected
    return actual == expected


def describe(values):
    return {
        "n": len(values),
        "mean_ms": statistics.mean(values),
        "std_ms": statistics.stdev(values),
        "median_ms": statistics.median(values),
        "min_ms": min(values),
        "max_ms": max(values),
    }


def compare_stats(actual, expected, source):
    for field, value in expected.items():
        observed = float(actual[field])
        # JSON was written with pandas' default 10 decimal places. CSV is full
        # precision. Relative tolerance covers arithmetic/serialization ulps only.
        absolute = 5.1e-11 if source == "summary.json" else 1e-12
        matches = (
            int(actual[field]) == value
            if field == "n"
            else math.isclose(observed, value, rel_tol=2e-15, abs_tol=absolute)
        )
        check(matches, f"{source}_statistics", (key(actual), field, observed, value))


def main():
    raw = read_jsonl(ROOT / "measurements.jsonl")
    csv_rows = read_csv("measurements.csv")
    manifest = read_json("manifest.json")
    validations = read_json("block-validations.json")
    references = read_json("validation-reference.json")
    conditions = set(itertools.product(SIZES, TASKS, PATHS))
    groups = defaultdict(list)
    for row in raw:
        groups[key(row)].append(row)
    check(len(raw) == len(csv_rows) == 3600, "sample_count")
    check(set(groups) == conditions, "condition_matrix")
    check(manifest["measurement_count"] == 3600, "manifest_measurement_count")
    check(manifest["settings"]["sizes"] == SIZES, "manifest_sizes")
    check(
        manifest["settings"]["runs"] == 30 and manifest["settings"]["blocks"] == 6,
        "manifest_repeats_blocks",
    )
    check(manifest["history_errors"] == [], "query_history_errors")
    check(manifest["status"] == "complete", "manifest_status", manifest["status"])
    check(
        set(references) == {f"{n}/{w}" for n, w in itertools.product(SIZES, TASKS)},
        "reference_matrix",
    )
    for row_num, (row, csv_row) in enumerate(zip(raw, csv_rows, strict=True), 1):
        for field in row.keys() | csv_row.keys():
            check(
                csv_equal(csv_row.get(field), row.get(field)),
                "jsonl_csv_field_equivalence",
                (row_num, field),
            )
    for condition, rows in groups.items():
        check(
            len(rows) == 30 and sorted(r["run"] for r in rows) == list(range(1, 31)),
            "condition_runs",
            condition,
        )
        check(
            Counter(r["block"] for r in rows) == {b: 5 for b in range(1, 7)},
            "condition_block_counts",
            condition,
        )
        for row in rows:
            check(row["block"] == (row["run"] - 1) // 5 + 1, "sample_block_assignment")
            reference = references[f"{row['rows']}/{row['workload']}"]
            check(row["output_rows"] == reference["rows"], "timed_output_row_counts")
            check(
                finite_nonnegative(row["elapsed_ms"]) and row["elapsed_ms"] > 0,
                "positive_elapsed_ms",
            )
            check(row.get("checksum") is None, "timed_checksum_not_recorded")
            remote = row["path"].startswith("snowflake")
            scope = "platform_query" if remote else "local_parquet_to_batches"
            check(row["metric_scope"] == scope, "metric_scope")
            if remote:
                check(bool(row.get("query_id")), "snowflake_query_id_present")
                check(
                    row["elapsed_ms"] == row.get("server_total_ms"),
                    "snowflake_elapsed_equals_server_total",
                )
                for field in COMPONENTS + ["server_bytes_scanned"]:
                    check(
                        finite_nonnegative(row.get(field)),
                        "snowflake_components_present_finite_nonnegative",
                        (row["query_id"], field),
                    )
    per_path = Counter(row["path"] for row in raw)
    check(per_path == {path: 600 for path in PATHS}, "samples_per_path")
    remote = [r for r in raw if r["path"].startswith("snowflake")]
    check(
        len(remote) == len({r["query_id"] for r in remote}) == 2400,
        "snowflake_unique_query_ids",
    )
    expected_blocks = {(*condition, b) for condition in conditions for b in range(1, 7)}
    block_keys = [(*key(v), v["block"]) for v in validations]
    check(
        len(validations) == len(set(block_keys)) == 720
        and set(block_keys) == expected_blocks,
        "block_validation_matrix",
    )
    for validation in validations:
        expected = references[f"{validation['rows']}/{validation['workload']}"]
        check(validation["checksum"] == expected, "block_full_checksum_reference_match")
        for field in ["rows", "sum_h", "sum_h2"]:
            check(
                type(validation["checksum"][field]) is int,
                "checksum_integer_precision",
                (*key(validation), field),
            )
    recomputed = {
        condition: describe([r["elapsed_ms"] for r in rows])
        for condition, rows in groups.items()
    }
    for source in ["summary.csv", "summary.json"]:
        summary = read_csv(source) if source.endswith("csv") else read_json(source)
        check(
            len(summary) == 120 and {key(r) for r in summary} == conditions,
            f"{source}_matrix",
        )
        for row in summary:
            compare_stats(row, recomputed[key(row)], source)
    component_rows = read_csv("snowflake-components.csv")
    component_keys = {(*key(r), r["component"]) for r in component_rows}
    expected_components = {
        (*condition, component)
        for condition in conditions
        if condition[2].startswith("snowflake")
        for component in COMPONENTS
    }
    check(
        len(component_rows) == 480 and component_keys == expected_components,
        "snowflake_component_matrix",
    )
    for row in component_rows:
        values = [r[row["component"]] for r in groups[key(row)]]
        expected = describe(values)
        compare_stats(
            row,
            {f: expected[f] for f in ["n", "mean_ms", "std_ms"]},
            "snowflake-components.csv",
        )
    sample_index = {(*key(r), r["run"]): r for r in raw}
    expected_worker_names = set()
    for n, work in itertools.product(SIZES, TASKS):
        reference = references[f"{n}/{work}"]
        name = f"reference-{n}-{work}.jsonl"
        expected_worker_names.add(name)
        entries = read_jsonl(ROOT / "workers" / name)
        check(
            len(entries) == 1
            and entries[0]["type"] == "warmup"
            and entries[0]["checksum"] == reference
            and entries[0]["output_rows"] == reference["rows"],
            "reference_worker_log",
            name,
        )
        for path, block in itertools.product(PATHS[:2], range(1, 7)):
            name = f"b{block}-{path}-{n}-{work}.jsonl"
            expected_worker_names.add(name)
            entries = read_jsonl(ROOT / "workers" / name)
            check(len(entries) == 6, "local_worker_log_length", name)
            check(
                entries[0]["type"] == "warmup"
                and entries[0]["checksum"] == reference
                and entries[0]["output_rows"] == reference["rows"],
                "local_worker_warmup_reference",
                name,
            )
            for run, entry in enumerate(entries[1:], 1):
                check(
                    entry["run"] == run and entry["type"] == "measurement",
                    "local_worker_run_sequence",
                    name,
                )
                index = (n, work, path, (block - 1) * 5 + run)
                expanded = {
                    **entry,
                    "rows": n,
                    "workload": work,
                    "path": path,
                    "block": block,
                    "run": index[-1],
                    "metric_scope": "local_parquet_to_batches",
                }
                check(
                    expanded == sample_index[index],
                    "local_worker_raw_equivalence",
                    index,
                )
    worker_files = list((ROOT / "workers").glob("*.jsonl"))
    check({p.name for p in worker_files} == expected_worker_names, "worker_file_matrix")
    stderr_files = list((ROOT / "workers").glob("*.stderr"))
    check(
        {p.with_suffix(".jsonl").name for p in stderr_files} == expected_worker_names,
        "worker_stderr_file_matrix",
    )
    for path in stderr_files:
        check(path.stat().st_size == 0, "worker_stderr_empty", path.name)
    report = {
        "status": "passed" if not issues else "failed",
        "assessment": "Share with caveats" if not issues else "Needs revision",
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_started_at_utc": manifest["started_at"],
        "run_completed_at_utc": manifest["completed_at"],
        "method": "Independent stdlib JSON/CSV/statistics recomputation; no production imports, remote queries, benchmarks or input hashes.",
        "counts": {
            "measurements_jsonl": len(raw),
            "measurements_csv": len(csv_rows),
            "conditions": len(groups),
            "repeats_per_condition": 30,
            "blocks_per_condition": 6,
            "repeats_per_block": 5,
            "samples_per_path": dict(per_path),
            "full_block_validations": len(validations),
            "references": len(references),
            "timed_row_counts_checked": len(raw),
            "timed_full_checksums_checked": 0,
            "snowflake_measurements": len(remote),
            "unique_snowflake_query_ids": len({r["query_id"] for r in remote}),
            "summary_statistic_comparisons": 120 * 6 * 2,
            "snowflake_component_rows": len(component_rows),
            "component_statistic_comparisons": len(component_rows) * 3,
            "local_worker_files": 240,
            "reference_worker_files": 20,
            "worker_samples_reconciled": 1200,
            "empty_stderr_files": len(stderr_files),
        },
        "checks_executed": dict(checks),
        "issues": issues,
        "numeric_policy": {
            "integers": "Exact Python ints and Decimal CSV comparison; no float conversion of checksum ints.",
            "raw_float_csv": "Exact equality after parsing CSV float.",
            "summary_json": "abs_tol=5.1e-11, rel_tol=2e-15 for 10-decimal JSON serialization.",
            "summary_and_components_csv": "abs_tol=1e-12, rel_tol=2e-15.",
        },
        "required_caveats": [
            "Full-output two-moment modular checksums match stored references in 720 untimed block validations; no per-timed-run full checksum verification.",
            "Timed output row counts agree for all 3600 samples; checksum method is non-cryptographic and full remote outputs are not retained.",
            "Local elapsed covers Parquet through all output batches; Snowflake elapsed is server total query time and excludes full client download and initial loading.",
            "Thirty repeats in six blocks describe this run; sample SD is not a confidence interval and observations may share cache or host state.",
            "This audit reconciles saved evidence. Source/input provenance and rendered chart/report review are separate primary-agent checks.",
        ],
        "evidence": [
            "measurements.jsonl",
            "measurements.csv",
            "validation-reference.json",
            "block-validations.json",
            "manifest.json",
            "summary.csv",
            "summary.json",
            "snowflake-components.csv",
            "workers/*.jsonl",
            "workers/*.stderr",
            "source/benchmarks/sweep_benchmark.py",
            "source/benchmarks/sweep_worker.py",
            "source/benchmarks/sweep_snowflake.py",
            "source/benchmarks/sweep_report.py",
        ],
        "reproduce": f"uv run --no-sync python {Path(__file__).resolve()}",
    }
    destination = ROOT / "independent-audit.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "counts": report["counts"],
                "issues": issues,
                "output": str(destination),
            },
            indent=2,
        )
    )
    raise SystemExit(bool(issues))


if __name__ == "__main__":
    main()
