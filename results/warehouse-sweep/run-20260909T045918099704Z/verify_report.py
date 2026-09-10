"""Check final report numbers, local links and stated comparison conclusions."""

import ast
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


def verify_workload_code(report):
    """Compare displayed snippets with the immutable measured source and inputs."""
    names = ["frame_workloads.py", "sweep_workloads.py", "frame_snowflake.py"]
    manifest = json.loads((FOLDER / "manifest.json").read_text())
    sources = {}
    for name in names:
        path = "benchmarks/" + name
        content = (FOLDER / "source" / path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == manifest["source_sha256"][path]
        sources[name] = content.decode()

    def function(file, name):
        return next(
            node
            for node in ast.parse(sources[file]).body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    transform = function("frame_workloads.py", "pandas_transform")
    branches = {
        node.test.comparators[0].value: node.body
        for node in transform.body
        if isinstance(node, ast.If)
    }
    queries = ast.literal_eval(
        function("frame_workloads.py", "sql_query").body[0].value
    )
    snowflake = function("frame_snowflake.py", "snowflake_query")
    clean_if = next(node for node in snowflake.body if isinstance(node, ast.If))
    snow_clean = ast.literal_eval(clean_if.body[0].value)
    dataset = next(
        row
        for row in json.loads((FOLDER / "datasets.json").read_text())
        if row["rows"] == 10**9
    )
    local = {
        key: "data/frame-benchmark/"
        + Path(dataset[key]).parent.name
        + "/"
        + Path(dataset[key]).name
        for key in ["fact", "dimension"]
    }
    loads = json.loads((FOLDER / "snowflake-setup.json").read_text())
    fact = next(row["table"] for row in loads if row["rows"] == 10**9)
    dimension = loads[0]["table"]
    snippets = re.findall(
        r"<!-- measured-code:(\w+):(\w+) -->\n```(?:python|sql)\n(.*?)\n```",
        report,
        re.S,
    )
    displayed = {(engine, work): code for engine, work, code in snippets}
    expected_keys = {
        (engine, work)
        for engine in ["pandas", "duckdb", "snowflake"]
        for work in branches
    }
    expected_keys |= {
        ("shared", name) for name in ["pandas_batches", "combine_aggregates"]
    }
    assert len(snippets) == len(displayed) == 14 and set(displayed) == expected_keys
    for work in branches:
        node = ast.parse(displayed["pandas", work]).body[0]
        assert isinstance(node, ast.FunctionDef) and node.name == work
        assert [ast.dump(n) for n in node.body] == [ast.dump(n) for n in branches[work]]
        duck = (
            queries[work]
            .replace("FROM sales", f"FROM read_parquet('{local['fact']}')")
            .replace("JOIN accounts", f"JOIN read_parquet('{local['dimension']}')")
        )
        snow = (
            (snow_clean if work == "clean_derive" else queries[work])
            .replace("FROM sales", f"FROM {fact}")
            .replace("JOIN accounts", f"JOIN {dimension}")
        )
        for engine, expected in [("duckdb", duck), ("snowflake", snow)]:
            assert displayed[engine, work].split() == expected.split(), (engine, work)
    for name in ["pandas_batches", "combine_aggregates"]:
        assert displayed["shared", name] == ast.get_source_segment(
            sources["sweep_workloads.py"], function("sweep_workloads.py", name)
        )
    return len(displayed)


def main():
    report = (FOLDER / "report.md").read_text()
    code_snippets = verify_workload_code(report)
    with (FOLDER / "summary.csv").open() as handle:
        summary = {
            (int(r["rows"]), r["workload"], r["path"]): r
            for r in csv.DictReader(handle)
        }
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
        "measured_code_snippets_verified": code_snippets,
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
