"""Check final report numbers, local links and stated comparison conclusions."""

import csv
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parents[2]
PATHS = [
    "pandas",
    "duckdb",
    "snowflake_xsmall",
    "snowflake_small",
    "snowflake_medium",
    "snowflake_large",
]
LABELS = {
    "결측치 처리 · 파생 컬럼": "clean_derive",
    "필터 · 파생 컬럼": "filter_project",
    "지역별 집계": "groupby",
    "조인 후 집계": "join_groupby",
}
DORIS = PROJECT / "results/doris/frames/run-20261006T000356880611Z"
SHARED_SIZES = [10**5, 10**6, 10**7]
ALL_SIZES = [*SHARED_SIZES, 10**8, 10**9]


def read_csv(path):
    with path.open() as handle:
        return list(csv.DictReader(handle))


def verify_doris_inputs():
    historical = {
        row["rows"]: row for row in json.loads((FOLDER / "datasets.json").read_text())
    }
    datasets = json.loads((DORIS / "datasets.json").read_text())
    assert len(datasets) == 3
    assert {row["rows"] for row in datasets} == set(SHARED_SIZES)
    for row in datasets:
        assert row == historical[row["rows"]], row["rows"]
        assert set(row["sha256"]) == {"sales.parquet", "accounts.parquet"}
    historical_manifest = json.loads((FOLDER / "manifest.json").read_text())
    manifest = json.loads((DORIS / "manifest.json").read_text())
    source = "benchmarks/frame_workloads.py"
    digest = manifest["source_sha256"][source]
    assert digest == historical_manifest["source_sha256"][source]
    for folder in [FOLDER, DORIS]:
        assert (
            hashlib.sha256((folder / "source" / source).read_bytes()).hexdigest()
            == digest
        )
    assert manifest["status"] == "complete" and manifest["measurement_count"] == 360
    assert manifest["settings"]["sizes"] == SHARED_SIZES
    assert manifest["settings"]["runs"] == 30 and manifest["settings"]["blocks"] == 6
    assert manifest["timing"] == (
        "SQL request to fully fetched/materialized/normalized int64 pandas result"
    )
    assert manifest["doris"]["enable_sql_cache"] is False
    assert manifest["doris"]["enable_query_cache"] is False
    reference = PROJECT / "results/pandas-duckdb/run-20260907T022808832856Z"
    for name, expected in manifest["reference_sha256"].items():
        assert hashlib.sha256((reference / name).read_bytes()).hexdigest() == expected
    validation = json.loads((DORIS / "validation.json").read_text())
    expected = json.loads((reference / "validation.json").read_text())
    historical_validation = json.loads(
        (FOLDER / "validation-reference.json").read_text()
    )
    keys = {f"{size}/{work}" for size in SHARED_SIZES for work in LABELS.values()}
    assert set(validation) == keys
    for key in keys:
        assert validation[key] == expected[key], key
        for field in ["columns", "rows"]:
            assert validation[key][field] == historical_validation[key][field], key
    return validation


def verify_doris_measurements(validation):
    raw = read_csv(DORIS / "measurements.csv")
    jsonl = [
        json.loads(line)
        for line in (DORIS / "measurements.jsonl").read_text().splitlines()
    ]
    assert len(raw) == len(jsonl) == 360
    counts, groups, identities = Counter(), {}, set()
    for row, saved in zip(raw, jsonl, strict=True):
        assert set(row) == set(saved)
        for field, value in saved.items():
            if isinstance(value, bool):
                assert row[field] == str(value)
            elif isinstance(value, (int, float)):
                assert float(row[field]) == value, field
            else:
                assert row[field] == value, field
        key = int(row["rows"]), row["workload"], row["engine"]
        block, run = int(row["block"]), int(row["run"])
        identity = (*key, run)
        assert identity not in identities, identity
        identities.add(identity)
        assert (
            key[0] in SHARED_SIZES and key[1] in LABELS.values() and key[2] == "doris"
        )
        assert row["mode"] == "warehouse" and row["validated"] == "True"
        assert 1 <= run <= 30 and block == (run - 1) // 5 + 1
        assert int(row["output_rows"]) == validation[f"{key[0]}/{key[1]}"]["rows"]
        elapsed = float(row["elapsed_ms"])
        assert math.isfinite(elapsed) and elapsed > 0
        assert math.isclose(
            elapsed,
            float(row["execute_roundtrip_ms"]) + float(row["fetch_dataframe_ms"]),
            abs_tol=1e-6,
        )
        counts[(*key, block)] += 1
        groups.setdefault(key, []).append(elapsed)
    expected_keys = {
        (size, work, "doris") for size in SHARED_SIZES for work in LABELS.values()
    }
    assert set(groups) == expected_keys and len(identities) == 360
    assert counts == Counter(
        {(*key, block): 5 for key in expected_keys for block in range(1, 7)}
    )
    return {
        key: {
            "n": len(values),
            "mean_ms": statistics.mean(values),
            "std_ms": statistics.stdev(values),
            "median_ms": statistics.median(values),
            "min_ms": min(values),
            "max_ms": max(values),
        }
        for key, values in groups.items()
    }


def verify_doris_csvs(historical, doris):
    checked = {}
    for name, expected in [
        ("doris-summary.csv", doris),
        ("doris-comparison.csv", {**historical, **doris}),
    ]:
        assert (FOLDER / name).is_file(), name
        rows = read_csv(FOLDER / name)
        actual = {(int(row["rows"]), row["workload"], row["path"]): row for row in rows}
        assert len(actual) == len(rows) == len(expected), name
        assert set(actual) == set(expected), name
        for key, row in actual.items():
            assert int(row["n"]) == 30
            for field in ["mean_ms", "std_ms", "median_ms", "min_ms", "max_ms"]:
                assert math.isclose(
                    float(row[field]), float(expected[key][field]), rel_tol=1e-12
                ), (name, key, field)
            scope = (
                "client_sql_to_full_pandas"
                if key[2] == "doris"
                else "platform_query"
                if key[2].startswith("snowflake")
                else "local_parquet_to_batches"
            )
            date = "2026-10-06" if key[2] == "doris" else "2026-09-09"
            assert row["metric_scope"] == scope and row["measured_date"] == date, (
                name,
                key,
            )
        checked[name] = len(rows)
    return checked


def verify_recommendations(report, summary):
    sizes = {
        "10만": 10**5,
        "100만": 10**6,
        "1,000만": 10**7,
        "1억": 10**8,
        "10억": 10**9,
    }
    candidates = {
        "pandas": ["pandas"],
        "DuckDB": ["duckdb"],
        "pandas·DuckDB": ["pandas", "duckdb"],
        "SF Medium": ["snowflake_medium"],
        "SF Medium·Large": ["snowflake_medium", "snowflake_large"],
        "SF Large (Medium도 후보)": ["snowflake_large", "snowflake_medium"],
        "SF Large": ["snowflake_large"],
    }
    checked = set()
    for line in report.splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] not in sizes:
            continue
        size = sizes[cells[0]]
        for work, cell in zip(LABELS.values(), cells[1:], strict=True):
            fastest = min(
                PATHS, key=lambda path: float(summary[size, work, path]["mean_ms"])
            )
            assert fastest in candidates[cell], (size, work, cell)
            checked.add((size, work))
    assert len(checked) == 20
    assert not re.search(r"```(?:python|sql)\b", report)
    return len(checked)


def main():
    report = (FOLDER / "report.md").read_text()
    with (FOLDER / "summary.csv").open() as handle:
        summary = {
            (int(r["rows"]), r["workload"], r["path"]): r
            for r in csv.DictReader(handle)
        }
    recommendation_cells = verify_recommendations(report, summary)
    doris = verify_doris_measurements(verify_doris_inputs())
    csv_checks = verify_doris_csvs(summary, doris)
    for phrase in [
        "기존 6개 경로",
        "전체 결과 수신·DataFrame 생성·int64 정규화",
        "동등한 서버 처리 시간",
        "1억·10억 행은 미측정",
        "SQL 요청",
    ]:
        assert phrase in report, phrase
    assert len(re.findall(r"^\| 작업 .*\| Doris 전체 반환† \|$", report, re.M)) == 5
    assert [
        int(value.replace(",", ""))
        for value in re.findall(r"^### ([\d,]+)행$", report, re.M)
    ] == ALL_SIZES
    seen = set()
    doris_seen, doris_missing = set(), set()
    scale_pairs = 0
    size = None
    for line in report.splitlines():
        if match := re.fullmatch(r"### ([\d,]+)행", line):
            size = int(match[1].replace(",", ""))
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] not in LABELS:
            continue
        work = LABELS[cells[0]]
        if len(cells) == 8 and "±" in cells[1]:
            for path, cell in zip(PATHS, cells[1:7], strict=True):
                row = summary[size, work, path]
                expected = f"{float(row['mean_ms']):,.2f} ± {float(row['std_ms']):,.2f}"
                assert cell == expected, (size, work, path, cell)
                seen.add((size, work, path))
            if size in SHARED_SIZES:
                row = doris[size, work, "doris"]
                expected = f"{row['mean_ms']:,.2f} ± {row['std_ms']:,.2f}"
                assert cells[7] == expected, (size, work, "doris", cells[7])
                doris_seen.add((size, work, "doris"))
            else:
                assert cells[7] == "미측정", (size, work, cells[7])
                doris_missing.add((size, work))
        if len(cells) == 4 and cells[1].endswith("배"):
            xs = float(summary[10**9, work, "snowflake_xsmall"]["mean_ms"])
            for path, cell in zip(PATHS[3:], cells[1:], strict=True):
                ratio = xs / float(summary[10**9, work, path]["mean_ms"])
                assert cell == f"{ratio:.2f}배", (work, path)
                scale_pairs += 1
    assert seen == set(summary) and scale_pairs == 12
    assert doris_seen == set(doris) and len(doris_missing) == 8
    images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report)
    assert images == [f"mean-sd-{n}.png" for n in [10**5, 10**6, 10**7, 10**8, 10**9]]
    links = 0
    for file in [
        FOLDER / "report.md",
        PROJECT / "README.md",
        PROJECT / "prd.md",
        PROJECT / "daily-development-report.md",
    ]:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", file.read_text()):
            if target.startswith(("https://", "http://", "#")):
                continue
            path = target.split("#")[0]
            assert (file.parent / path).exists(), (file.name, target)
            links += 1
    raw = [
        json.loads(line)
        for line in (FOLDER / "measurements.jsonl").read_text().splitlines()
    ]
    for row in raw:
        scope = (
            "platform_query"
            if row["path"].startswith("snowflake")
            else "local_parquet_to_batches"
        )
        assert row["metric_scope"] == scope
        if scope == "platform_query":
            assert row["elapsed_ms"] == row["server_total_ms"]
    blocks = {}
    for row in raw:
        key = row["rows"], row["workload"], row["path"], row["block"]
        blocks.setdefault(key, []).append(row["elapsed_ms"])
    for size in [10**5, 10**6, 10**7]:
        for work in LABELS.values():
            winner = min(
                PATHS, key=lambda path: float(summary[size, work, path]["mean_ms"])
            )
            expected = "duckdb" if "groupby" in work else "pandas"
            assert winner == expected
    for work in LABELS.values():
        for block in range(1, 7):
            winner = min(
                PATHS,
                key=lambda path: statistics.mean(blocks[10**9, work, path, block]),
            )
            assert winner == "snowflake_large", (work, block)
    reversals = sum(
        statistics.mean(blocks[10**9, "join_groupby", "duckdb", b])
        > statistics.mean(blocks[10**9, "join_groupby", "snowflake_xsmall", b])
        for b in range(1, 7)
    )
    assert reversals == 2
    assert all(
        row[k] == 0
        for row in raw
        if row["path"].startswith("snowflake")
        for k in ["server_provisioning_queue_ms", "server_overload_queue_ms"]
    )
    manifest = json.loads((FOLDER / "manifest.json").read_text())
    for path, size in zip(
        PATHS[2:], ["X-Small", "Small", "Medium", "Large"], strict=True
    ):
        wh = manifest["warehouses"][path]
        assert wh["name"] == "FRAME_SWEEP_" + path.split("_")[1].upper()
        assert wh["size"] == size and wh["auto_resume"] == "true"
    evidence = [
        "report.md",
        *images,
        "independent-audit.json",
        "provenance-audit.json",
        "visual-review.json",
        "doris-summary.csv",
        "doris-comparison.csv",
        "doris-render-audit.json",
        "doris-visual-review.json",
    ]
    result = {
        "status": "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "report_mean_sd_pairs": len(seen),
        "doris_mean_sd_pairs": len(doris_seen),
        "doris_unmeasured_cells": len(doris_missing),
        "doris_saved_measurements": 360,
        "doris_saved_conditions": len(doris),
        "doris_csv_rows_verified": csv_checks,
        "doris_shared_input_metadata_and_sha256": "passed for all three shared sizes",
        "doris_workload_source_sha256": "identical to the preserved warehouse sweep",
        "metric_scopes": "local batches, Snowflake platform query and Doris full pandas return remain distinct",
        "warehouse_ratio_values": scale_pairs,
        "png_references": len(images),
        "recommendation_cells_verified": recommendation_cells,
        "existing_local_links_checked": links,
        "billion_large_winner_in_each_workload_and_block": 24,
        "billion_duckdb_join_slower_than_xsmall_blocks": reversals,
        "warehouse_name_size_mapping": "passed against saved manifest",
        "sha256": {
            name: hashlib.sha256((FOLDER / name).read_bytes()).hexdigest()
            for name in evidence
        },
        "limits": "Checks saved evidence and Markdown links/numbers; visual PNG inspection is recorded separately. No new Snowflake queries or measurements.",
    }
    (FOLDER / "report-audit.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({key: value for key, value in result.items() if key != "sha256"}))


if __name__ == "__main__":
    main()
