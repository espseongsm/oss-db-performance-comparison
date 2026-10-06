"""Reject partial or incompatible historical merges without a live Doris server."""

import csv
import json
from pathlib import Path

import pytest

from scripts import doris_comparison as comparison


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def sql_fixture(folder: Path, engine="doris", missing=None):
    save(folder / "dataset.json", {
        "rows": 1_000_000_000, "actual_rows": 1_000_000_000,
        "dimension_rows": 1_000_000, "actual_dimension_rows": 1_000_000,
        "schema_version": "v2",
    })
    queries = {query: {"digest": query, "row_count": 1} for query in comparison.QUERIES}
    save(folder / "validation.json", {"engine": engine, "rows": 1_000_000_000, "queries": queries})
    records = [
        {"engine": engine, "query": query, "iteration": iteration,
         "elapsed_ms": iteration, **queries[query]}
        for query in comparison.QUERIES if query != missing
        for iteration in range(1, 31)
    ]
    (folder / "measurements.jsonl").write_text("\n".join(json.dumps(row) for row in records))
    return queries


def test_sql_statistics_use_raw_and_leave_missing_join(tmp_path):
    queries = sql_fixture(tmp_path, "sqlite", missing="join")
    rows, notes = comparison.sql_records(tmp_path, "sqlite", queries, allow_missing_join=True)
    assert len(rows) == 3
    assert rows[0]["mean_ms"] == 15.5
    assert rows[0]["p95_ms"] == 29
    assert "missing" in notes[0]


def test_sql_v3_layout_accepts_matching_logical_rows_and_checksums(tmp_path):
    queries = sql_fixture(tmp_path)
    path = tmp_path / "dataset.json"
    dataset = comparison.read(path)
    dataset["schema_version"] = "v3"
    save(path, dataset)
    rows, _ = comparison.sql_records(tmp_path, "doris", queries)
    assert len(rows) == 4


def test_percentiles_preserve_each_experiments_definition():
    values = list(range(1, 31))
    assert comparison.stats(values)["p95_ms"] == 29
    assert comparison.stats(values, linear_p95=True)["p95_ms"] == pytest.approx(28.55)


def complete_sql_fixture(reference: Path, doris: Path):
    for engine in comparison.ENGINES:
        sql_fixture(reference / engine, engine)
    queries = sql_fixture(doris / "doris")
    save(doris / "doris/summary.json", {"engine": "doris", "rows": 1_000_000_000,
                                        "queries": {key: {**value, "runs": 30} for key, value in queries.items()}})
    save(doris / "experiment-manifest-baseline-doris.json", {
        "rows": 1_000_000_000, "runs": 30, "engines": ["doris"], "completed_at": "2026-10-06T00:00:00Z"
    })


def test_sql_merge_requires_complete_doris_manifest_and_summary(tmp_path):
    reference, doris = tmp_path / "old", tmp_path / "new"
    complete_sql_fixture(reference, doris)
    rows, _ = comparison.sql_comparison(reference, doris, "baseline")
    assert len(rows) == 20
    path = doris / "experiment-manifest-baseline-doris.json"
    manifest = comparison.read(path)
    manifest["runs"] = 1
    save(path, manifest)
    with pytest.raises(ValueError, match="manifest"):
        comparison.sql_comparison(reference, doris, "baseline")
    manifest["runs"] = 30
    save(path, manifest)
    summary = comparison.read(doris / "doris/summary.json")
    summary["queries"]["small"]["runs"] = 1
    save(doris / "doris/summary.json", summary)
    with pytest.raises(ValueError, match="summary"):
        comparison.sql_comparison(reference, doris, "baseline")


@pytest.mark.parametrize("change", ["partial", "digest", "dataset", "duplicate"])
def test_sql_rejects_incompatible_or_partial_results(tmp_path, change):
    queries = sql_fixture(tmp_path)
    raw = tmp_path / "measurements.jsonl"
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    if change == "partial":
        records.pop()
    elif change == "digest":
        records[0]["digest"] = "other"
    elif change == "duplicate":
        records[0]["iteration"] = 2
    else:
        dataset = comparison.read(tmp_path / "dataset.json")
        dataset["actual_rows"] = 10_000
        save(tmp_path / "dataset.json", dataset)
    raw.write_text("\n".join(json.dumps(row) for row in records))
    with pytest.raises(ValueError):
        comparison.sql_records(tmp_path, "doris", queries)


def frames_fixture(reference: Path, doris: Path):
    settings = {"sizes": [100_000], "runs": 30, "blocks": 6, "threads": 4, "memory_gb": 8, "seed": 20260907}
    manifest = {"status": "complete", "settings": settings, "source_sha256": {"benchmarks/frame_workloads.py": "same-source"}}
    datasets = [{"rows": 100_000, "version": 1, "seed": 20260907, "columns": 8,
                 "dimension_rows": 100_000, "compression": "snappy", "row_group_size": 100_000,
                 "sha256": {"sales.parquet": "same-input", "accounts.parquet": "same-dimension"}}]
    validation = {f"100000/{workload}": {"rows": 50, "hash_sum": "same-result"} for workload in comparison.WORKLOADS}
    for folder, engines, modes in ((reference, ["pandas", "duckdb"], ["memory", "parquet"]), (doris, ["doris"], ["warehouse"])):
        for name, data in (("manifest.json", manifest), ("datasets.json", datasets), ("validation.json", validation)):
            save(folder / name, data)
        records = [{"rows": 100_000, "mode": mode, "engine": engine, "workload": workload,
                    "run": run, "block": (run - 1) // 5 + 1, "elapsed_ms": run,
                    "validated": True, "output_rows": 50}
                   for engine in engines for mode in modes for workload in comparison.WORKLOADS for run in range(1, 31)]
        with (folder / "measurements.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    manifest = comparison.read(doris / "manifest.json")
    manifest["reference_sha256"] = {name: comparison.sha256(reference / name) for name in ("datasets.json", "validation.json", "manifest.json")}
    save(doris / "manifest.json", manifest)


def test_preprocessing_keeps_input_paths_separate(tmp_path):
    reference, doris = tmp_path / "old", tmp_path / "new"
    frames_fixture(reference, doris)
    rows, notes = comparison.frame_comparison(reference, doris)
    assert len(rows) == 20
    assert rows[0]["p95_ms"] == pytest.approx(28.55)
    assert {row["mode"] for row in rows} == {"warehouse", "parquet", "memory"}
    assert {row["input_path"] for row in rows} == {
        "preloaded server tables", "local Parquet scan", "preloaded local DataFrame"
    }
    assert any("Snowflake" in note and "not" in note for note in notes)


@pytest.mark.parametrize("change", ["failed", "fingerprint", "checksum", "partial"])
def test_preprocessing_rejects_failed_or_incompatible_results(tmp_path, change):
    reference, doris = tmp_path / "old", tmp_path / "new"
    frames_fixture(reference, doris)
    if change == "partial":
        path = doris / "measurements.csv"
        path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    else:
        path = doris / {"failed": "manifest.json", "fingerprint": "validation.json", "checksum": "datasets.json"}[change]
        data = comparison.read(path)
        if change == "failed":
            data["status"] = "failed"
        elif change == "fingerprint":
            data["100000/groupby"]["hash_sum"] = "other-result"
        else:
            data[0]["sha256"]["sales.parquet"] = "other-input"
        save(path, data)
    with pytest.raises(ValueError):
        comparison.frame_comparison(reference, doris)


def test_existing_output_is_rejected_without_reading_inputs(tmp_path):
    with pytest.raises(SystemExit):
        comparison.main(["--suite", "sql", "--doris-dir", str(tmp_path / "missing"), "--output-dir", str(tmp_path)])


def test_environment_provenance_preserves_observed_deployment_and_sources(tmp_path):
    reference, doris = tmp_path / "old", tmp_path / "new"
    frames_fixture(reference, doris)
    manifest = comparison.read(doris / "manifest.json")
    manifest["doris"] = {"version": "doris-4.1.3", "exec_mem_limit": 8 * 1024**3}
    save(doris / "manifest.json", manifest)
    deployment = {"captured_at": "2026-10-06T00:00:00Z", "image": "apache/doris:4.1.3",
                  "nano_cpus": 4_000_000_000, "memory_bytes": 8 * 1024**3}
    save(doris / "deployment.json", deployment)
    environments = comparison.environments({"reference_dir": str(reference), "doris_dir": str(doris)}, "frames", "warm")
    historical = next(row for row in environments if row["source"] == "historical")
    current = next(row for row in environments if row["source"] == "new" and "manifest" in row)
    captured = next(row for row in environments if "recorded_deployment" in row)
    assert historical["source_sha256"] == {"benchmarks/frame_workloads.py": "same-source"}
    assert current["doris"] == manifest["doris"]
    assert captured["recorded_deployment"] == deployment
    assert captured["deployment_sha256"] == comparison.sha256(doris / "deployment.json")


def test_sql_environment_dates_are_profile_specific(tmp_path):
    reference, doris = tmp_path / "old", tmp_path / "new"
    complete_sql_fixture(reference, doris)
    save(reference / "experiment-manifest-optimized-clickhouse.json", {
        "rows": 1_000_000_000, "completed_at": "optimized-date", "engines": ["clickhouse"]
    })
    environments = comparison.environments({"reference_dir": str(reference), "doris_dir": str(doris)}, "sql", "baseline")
    assert all(row.get("completed_at") != "optimized-date" for row in environments)
