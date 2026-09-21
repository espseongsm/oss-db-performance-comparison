"""Read-only independent audit of this completed run; writes audit evidence only."""

import csv
import hashlib
import itertools
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

FOLDER = Path(__file__).resolve().parent


def read_json(name):
    return json.loads((FOLDER / name).read_text())


def main():
    manifest = read_json("manifest.json")
    settings = manifest["settings"]
    assert manifest["status"] == "complete"
    raw = [
        json.loads(line)
        for line in (FOLDER / "measurements.jsonl").read_text().splitlines()
    ]
    with (FOLDER / "measurements.csv").open() as handle:
        saved = list(csv.DictReader(handle))
    assert len(raw) == len(saved) == manifest["measurement_count"] == 1080
    for a, b in zip(raw, saved, strict=True):
        assert set(a) == set(b)
        for key, value in a.items():
            if isinstance(value, (dict, list)):
                assert value == json.loads(b[key])
            elif value is None:
                assert b[key] == ""
            elif isinstance(value, bool):
                assert b[key].lower() == str(value).lower()
            elif isinstance(value, (float, int)):
                assert math.isclose(value, float(b[key]), rel_tol=1e-12, abs_tol=1e-9)
            else:
                assert value == b[key]
    calls = read_json("calls.json")
    assert len(calls) == len({c["call_id"] for c in calls}) == 216
    call_map = {c["call_id"]: c for c in calls}
    for call in calls:
        runtime = call["runtime"]
        assert runtime["warehouse"].strip('"') == settings["warehouse"]
        assert runtime["versions"] == manifest["packages"]
        assert runtime["use_cached_result"].lower() == "false"
    groups, query_ids, max_errors = defaultdict(list), [], defaultdict(float)
    references = read_json("validation.json")
    for row in raw:
        key = (row["rows"], row["workload"], row["engine"])
        groups[key].append(row)
        assert row["mode"] == "warehouse" and row["validated"]
        assert row["fingerprint"] == references[f"{key[0]}/{key[1]}"]
        assert row["output_rows"] == row["fingerprint"]["rows"]
        assert row["block"] == (row["run"] - 1) // 5 + 1
        assert all(
            call_map[row["call_id"]][k] == row[k]
            for k in ("rows", "workload", "engine", "block")
        )
        assert (row["input_ms"] is None) == (row["engine"] == "snowflake")
        assert row["elapsed_ms"] > 0
        total = (
            (row["input_ms"] or 0) + row["setup_ms"] + row["compute_to_dataframe_ms"]
        )
        assert math.isclose(row["elapsed_ms"], total, rel_tol=1e-12)
        assert set(row["server_queries"]) == set(row["query_ids"])
        assert len(row["query_ids"]) == (
            2
            if row["workload"] == "join_groupby" and row["engine"] != "snowflake"
            else 1
        )
        query_ids.extend(row["query_ids"])
        for query in row["server_queries"].values():
            assert query is not None and len(query) == 6
            assert all(value is not None and value >= 0 for value in query.values())
    expected = set(
        itertools.product(
            settings["sizes"],
            ("clean_derive", "filter_project", "groupby", "join_groupby"),
            ("pandas", "duckdb", "snowflake"),
        )
    )
    assert set(groups) == expected
    assert len(query_ids) == len(set(query_ids)) == 1260
    assert manifest["history_errors"] == []
    with (FOLDER / "summary.csv").open() as handle:
        summary = list(csv.DictReader(handle))
    assert len(summary) == 36
    for saved_row in summary:
        rows = groups[
            (int(saved_row["rows"]), saved_row["workload"], saved_row["engine"])
        ]
        assert sorted(r["run"] for r in rows) == list(range(1, 31))
        assert Counter(r["block"] for r in rows) == dict.fromkeys(range(1, 7), 5)
        values = [r["elapsed_ms"] for r in rows]
        calculated = dict(
            mean_ms=statistics.mean(values),
            std_ms=statistics.stdev(values),
            median_ms=statistics.median(values),
            min_ms=min(values),
            max_ms=max(values),
            setup_mean_ms=statistics.mean(r["setup_ms"] for r in rows),
            compute_mean_ms=statistics.mean(r["compute_to_dataframe_ms"] for r in rows),
        )
        if rows[0]["engine"] != "snowflake":
            calculated["input_mean_ms"] = statistics.mean(r["input_ms"] for r in rows)
        else:
            assert saved_row["input_mean_ms"] == ""
        assert int(saved_row["n"]) == len(values) == 30
        for name, value in calculated.items():
            actual = float(saved_row[name])
            assert math.isclose(value, actual, rel_tol=1e-12, abs_tol=1e-9)
            max_errors[name] = max(max_errors[name], abs(value - actual))
    for name, digest in manifest["source_sha256"].items():
        assert (
            hashlib.sha256((FOLDER / "source" / name).read_bytes()).hexdigest()
            == digest
        )
    inputs = {}
    for dataset in read_json("datasets.json"):
        for field in ("fact", "dimension"):
            path = Path(dataset[field])
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            assert digest == dataset["sha256"][path.name]
            inputs[str(path)] = digest
    qa = read_json("qa.json")
    qa["independent_audit"] = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "method": "Python standard-library csv/json/statistics.stdev (n-1); no Snowflake query or benchmark rerun",
        "raw_csv_equals_jsonl": True,
        "measurements": len(raw),
        "engine_counts": dict(Counter(r["engine"] for r in raw)),
        "conditions": len(groups),
        "runs_per_condition": 30,
        "blocks_per_condition": 6,
        "calls_checked": len(calls),
        "unique_child_queries": len(set(query_ids)),
        "source_hashes_checked": len(manifest["source_sha256"]),
        "input_hashes_checked": len(inputs),
        "max_absolute_statistic_error_ms": dict(max_errors),
        "validation_limit": "Per-sample full-result fingerprints are persisted and match common references; complete result bodies are not retained.",
    }
    (FOLDER / "qa.json").write_text(json.dumps(qa, indent=2) + "\n")
    print(json.dumps(qa["independent_audit"]))


if __name__ == "__main__":
    main()
