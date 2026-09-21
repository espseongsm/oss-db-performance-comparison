"""Check final report numbers, local links and stated comparison conclusions."""

import csv
import hashlib
import json
import re
import statistics
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
    seen = set()
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
        if len(cells) == 7 and "±" in cells[1]:
            for path, cell in zip(PATHS, cells[1:], strict=True):
                row = summary[size, work, path]
                expected = f"{float(row['mean_ms']):,.2f} ± {float(row['std_ms']):,.2f}"
                assert cell == expected, (size, work, path, cell)
                seen.add((size, work, path))
        if len(cells) == 4 and cells[1].endswith("배"):
            xs = float(summary[10**9, work, "snowflake_xsmall"]["mean_ms"])
            for path, cell in zip(PATHS[3:], cells[1:], strict=True):
                ratio = xs / float(summary[10**9, work, path]["mean_ms"])
                assert cell == f"{ratio:.2f}배", (work, path)
                scale_pairs += 1
    assert seen == set(summary) and scale_pairs == 12
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
    ]
    result = {
        "status": "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "report_mean_sd_pairs": len(seen),
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
