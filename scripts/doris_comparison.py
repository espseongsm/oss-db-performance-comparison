"""Build a fresh comparison from preserved results and a complete Doris-only run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ("clickhouse", "duckdb", "postgres", "sqlite")
QUERIES = ("small", "medium", "large", "join")
WORKLOADS = ("filter_project", "clean_derive", "groupby", "join_groupby")
DATA_KEYS = ("version", "seed", "rows", "columns", "dimension_rows", "compression", "row_group_size", "sha256")


def read(path: Path):
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def stats(samples: list[float], *, linear_p95: bool = False) -> dict:
    if len(samples) != 30 or any(not math.isfinite(x) or x < 0 for x in samples):
        raise ValueError("Each full comparison case requires 30 finite nonnegative timings")
    values = sorted(samples)
    p95_index = (len(values) - 1) * .95
    p95 = (
        values[math.floor(p95_index)] * (1 - p95_index % 1)
        + values[math.ceil(p95_index)] * (p95_index % 1)
        if linear_p95 else values[math.ceil(len(values) * .95) - 1]
    )
    return {
        "runs": 30,
        "mean_ms": statistics.fmean(values),
        "stddev_ms": statistics.stdev(values),
        "p50_ms": statistics.median(values),
        "p95_ms": p95,
    }


def sql_records(folder: Path, engine: str, references: dict, *, allow_missing_join: bool = False):
    dataset = read(folder / "dataset.json")
    if any(dataset.get(key) != value for key, value in {
        "rows": 1_000_000_000, "actual_rows": 1_000_000_000,
        "dimension_rows": 1_000_000, "actual_dimension_rows": 1_000_000,
    }.items()):
        raise ValueError(f"SQL dataset does not match the historical billion-row experiment: {folder}")
    if dataset.get("schema_version") not in ("v2", "v3"):
        raise ValueError(f"Unsupported SQL schema version: {folder}")
    validation = read(folder / "validation.json")
    if validation.get("engine") != engine or validation.get("rows") != dataset["rows"]:
        raise ValueError(f"SQL validation metadata mismatch: {folder}")
    raw = [json.loads(line) for line in (folder / "measurements.jsonl").read_text().splitlines() if line]
    if any(row.get("engine") != engine or row.get("query") not in QUERIES for row in raw):
        raise ValueError(f"Unexpected SQL measurement case: {folder}")
    result, notes = [], []
    for query in QUERIES:
        records = [row for row in raw if row["query"] == query]
        if not records and allow_missing_join and query == "join":
            notes.append("optimized SQLite join: missing; no value imputed")
            continue
        expected = references[query]
        validated = validation["queries"].get(query, {})
        if any(validated.get(key) != expected[key] for key in ("digest", "row_count")):
            raise ValueError(f"SQL validation checksum mismatch: {engine}/{query}")
        if len(records) != 30 or {row["iteration"] for row in records} != set(range(1, 31)):
            raise ValueError(f"Incomplete SQL measurements: {engine}/{query}")
        if any(any(row.get(key) != expected[key] for key in ("digest", "row_count")) for row in records):
            raise ValueError(f"SQL measured checksum mismatch: {engine}/{query}")
        result.append({"rows": dataset["rows"], "workload": query, "engine": engine,
                       "mode": "sql", **stats([row["elapsed_ms"] for row in records])})
    return result, notes


def sql_comparison(reference: Path, doris: Path, profile: str):
    manifest = read(doris / f"experiment-manifest-{profile}-doris.json")
    if (
        manifest.get("rows") != 1_000_000_000 or manifest.get("runs") != 30
        or manifest.get("engines") != ["doris"] or not manifest.get("completed_at")
    ):
        raise ValueError("A completed Doris-only billion-row, 30-run SQL manifest is required")
    references = read(reference / "clickhouse" / "validation.json")["queries"]
    rows, notes = [], []
    relative = Path() if profile == "baseline" else Path(profile)
    for engine, root in [*((engine, reference) for engine in ENGINES), ("doris", doris)]:
        folder = root / relative / engine
        records, exclusions = sql_records(
            folder, engine, references, allow_missing_join=(profile == "optimized" and engine == "sqlite")
        )
        for record in records:
            record.update(profile=profile, source="new" if engine == "doris" else "historical",
                          input_path="preloaded SQL tables")
        rows.extend(records)
        notes.extend(exclusions)
        summary_path = folder / "summary.json"
        if engine == "doris":
            summary = read(summary_path)
            if summary.get("rows") != 1_000_000_000 or summary.get("engine") != "doris" or any(
                summary.get("queries", {}).get(query, {}).get("runs") != 30
                or summary["queries"][query].get("digest") != references[query]["digest"]
                for query in QUERIES
            ):
                raise ValueError("Doris SQL summary is incomplete or incompatible")
        if summary_path.exists() and read(summary_path).get("rows") != 1_000_000_000:
            notes.append(f"{profile}/{engine}: stale summary excluded; statistics recomputed from verified raw measurements")
    notes.extend([
        "Historical engines are not rerun. Raw measurements, dataset counts and result checksums are checked before comparison.",
        "Historical and Doris runs occurred at different times. Compare deployment, versions, CPU and memory metadata before interpreting rankings.",
        "Doris FE and BE resources must be considered together. This report does not infer resource equality from matching query results.",
    ])
    return rows, notes


def frame_records(folder: Path, manifest: dict, validation: dict, *, doris: bool):
    with (folder / "measurements.csv").open(newline="") as handle:
        raw = list(csv.DictReader(handle))
    groups = defaultdict(list)
    sizes = manifest["settings"]["sizes"]
    modes, engines = (["warehouse"], ["doris"]) if doris else (["memory", "parquet"], ["pandas", "duckdb"])
    for row in raw:
        key = (int(row["rows"]), row["mode"], row["workload"], row["engine"])
        if key[0] not in sizes or key[1] not in modes or key[2] not in WORKLOADS or key[3] not in engines:
            raise ValueError(f"Unexpected preprocessing case: {key}")
        expected = validation[f"{key[0]}/{key[2]}"]
        if row.get("validated", "").lower() != "true" or int(row["output_rows"]) != expected["rows"]:
            raise ValueError(f"Preprocessing measured result mismatch: {key}")
        groups[key].append(row)
    if len(groups) != len(sizes) * len(modes) * len(engines) * len(WORKLOADS):
        raise ValueError(f"Incomplete preprocessing measurement matrix: {folder}")
    result = []
    for (size, mode, workload, engine), records in sorted(groups.items()):
        if len(records) != 30 or {int(row["run"]) for row in records} != set(range(1, 31)):
            raise ValueError(f"Incomplete preprocessing repetitions: {size}/{mode}/{workload}/{engine}")
        if {int(row["block"]) for row in records} != set(range(1, 7)) or any(
            sum(int(row["block"]) == block for row in records) != 5 for block in range(1, 7)
        ):
            raise ValueError(f"Preprocessing block layout mismatch: {size}/{mode}/{workload}/{engine}")
        result.append({"rows": size, "workload": workload, "engine": engine, "mode": mode,
                       "profile": "warm", "source": "new" if doris else "historical",
                       "input_path": {"warehouse": "preloaded server tables", "memory": "preloaded local DataFrame", "parquet": "local Parquet scan"}[mode],
                       **stats([float(row["elapsed_ms"]) for row in records], linear_p95=True)})
    return result


def frame_comparison(reference: Path, doris: Path):
    old, new = read(reference / "manifest.json"), read(doris / "manifest.json")
    if old.get("status") != "complete" or new.get("status") != "complete":
        raise ValueError("Both preprocessing runs must be complete")
    for key in ("sizes", "runs", "blocks", "threads", "memory_gb", "seed"):
        if new["settings"].get(key) != old["settings"].get(key):
            raise ValueError(f"Preprocessing experiment setting mismatch: {key}")
    if old["settings"].get("runs") != 30 or old["settings"].get("blocks") != 6:
        raise ValueError("Full preprocessing comparison requires 30 repetitions in 6 blocks")
    for name in ("datasets.json", "validation.json", "manifest.json"):
        if new.get("reference_sha256", {}).get(name) != sha256(reference / name):
            raise ValueError(f"Historical reference checksum mismatch: {name}")
    workload_source = "benchmarks/frame_workloads.py"
    if not old.get("source_sha256", {}).get(workload_source) or (
        new.get("source_sha256", {}).get(workload_source) != old["source_sha256"][workload_source]
    ):
        raise ValueError("Preprocessing workload source checksum mismatch")
    datasets = {item["rows"]: item for item in read(reference / "datasets.json")}
    new_datasets = {item["rows"]: item for item in read(doris / "datasets.json")}
    if set(new_datasets) != set(datasets) or set(datasets) != set(old["settings"]["sizes"]):
        raise ValueError("Preprocessing dataset sizes mismatch")
    for size, dataset in new_datasets.items():
        if any(dataset.get(key) != datasets[size].get(key) for key in DATA_KEYS):
            raise ValueError(f"Preprocessing dataset checksum or metadata mismatch: {size}")
    expected = read(reference / "validation.json")
    if read(doris / "validation.json") != expected:
        raise ValueError("Preprocessing output fingerprints mismatch")
    rows = frame_records(reference, old, expected, doris=False) + frame_records(doris, new, expected, doris=True)
    notes = [
        "Historical pandas/DuckDB measurements are preserved. Input checksums, workload source and output fingerprints match the Doris run.",
        "Local Parquet, preloaded local DataFrame and preloaded server table paths are labeled separately. They have different I/O and deployment boundaries.",
        "No paired-block significance or bootstrap ratio is calculated across historical and new runs; these are descriptive timings from different dates.",
        "This comparison reuses the historical full-DataFrame pandas/DuckDB run. The existing Snowflake DW-size sweep uses batched local outputs and server-side SQL timing; it is not combined because the timing boundaries differ.",
        "Doris server memory and client RSS are different measures; this chart compares elapsed time only.",
    ]
    return rows, notes


def environments(inputs: dict, suite: str, profile: str) -> list[dict]:
    """Retain recorded dates and resources; absent metadata remains unknown."""
    records = []
    for source, root_name in (("historical", "reference_dir"), ("new", "doris_dir")):
        root = Path(inputs[root_name])
        paths = [root / "manifest.json"] if suite == "frames" else sorted(root.glob("experiment-manifest*.json"))
        for path in paths:
            if not path.exists():
                continue
            manifest = read(path)
            if suite == "sql" and manifest.get("rows") != 1_000_000_000:
                continue
            if suite == "sql" and ("optimized" in path.name) != (profile == "optimized"):
                continue
            records.append({"source": source, "manifest": str(path), "manifest_sha256": sha256(path), **{
                key: manifest[key] for key in ("started_at", "completed_at", "engines", "platform", "machine", "python", "versions", "settings", "source_sha256", "cpu_logical_count", "cpu_physical_count", "physical_memory_bytes", "initial_system", "final_system", "cache", "timing", "excluded", "memory", "doris") if key in manifest
            }})
        deployment_paths = [root / "deployment.json"]
        if suite == "sql":
            relative = Path() if profile == "baseline" else Path(profile)
            deployment_paths.append(root / relative / "doris" / "deployment.json")
            for engine in (*ENGINES, "doris"):
                path = root / relative / engine / "optimization.json"
                if path.exists():
                    records.append({"source": source, "engine": engine, "physical_design": read(path)})
        for path in deployment_paths:
            if path.exists():
                records.append({"source": source, "deployment_file": str(path),
                                "deployment_sha256": sha256(path), "recorded_deployment": read(path)})
    return records


def write_comparison(output: Path, rows: list[dict], notes: list[str], suite: str, profile: str, inputs: dict):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, NullFormatter

    output.mkdir(parents=True, exist_ok=False)
    with (output / "comparison.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    cases = sorted({(row["rows"], row["workload"]) for row in rows})
    columns = 2 if suite == "sql" else 4
    fig, axes = plt.subplots(math.ceil(len(cases) / columns), columns, figsize=(columns * 5, math.ceil(len(cases) / columns) * 3.8), squeeze=False)
    for ax, (size, workload) in zip(axes.flat, cases, strict=False):
        records = [row for row in rows if row["rows"] == size and row["workload"] == workload]
        labels = [row["engine"] + (f" / {row['mode']}" if suite == "frames" else "") for row in records]
        means = [row["mean_ms"] for row in records]
        deviations = [row["stddev_ms"] for row in records]
        # A negative lower SD bound cannot be shown on a logarithmic axis.
        floor = min(mean for mean in means if mean > 0) / 10
        lower = [min(sd, max(0, mean - floor)) for mean, sd in zip(means, deviations, strict=True)]
        ax.barh(labels, means, xerr=[lower, deviations], error_kw={"ecolor": "#222222", "capsize": 3}, color=["#c77931" if row["source"] == "new" else "#3975b9" for row in records])
        ax.set_xscale("log")
        low, high = ax.get_xlim()
        candidates = [factor * 10**power for power in range(math.floor(math.log10(low)) - 1, math.ceil(math.log10(high)) + 1) for factor in (1, 2, 5)]
        ticks = [value for value in candidates if low <= value <= high]
        if not ticks:
            ticks = [max(value for value in candidates if value < low), min(value for value in candidates if value > high)]
        label_limit = 4 if suite == "sql" else 5
        if len(ticks) > label_limit:
            ticks = [ticks[round(index * (len(ticks) - 1) / (label_limit - 1))] for index in range(label_limit)]
        ax.set_xticks(ticks)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.9f}".rstrip("0").rstrip(".")))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlabel("Mean ± SD elapsed ms (log scale)")
        ax.set_title(f"{size:,} rows · {workload}")
        ax.grid(axis="x", alpha=.2)
        ax.invert_yaxis()
    for ax in list(axes.flat)[len(cases):]:
        ax.set_visible(False)
    fig.suptitle(f"Doris + historical {suite} / {profile}: descriptive timings", fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, .97))
    fig.savefig(output / "comparison.png", dpi=160)
    plt.close(fig)
    lines = [f"# Doris + historical {suite} comparison ({profile})", "", "![Measured comparison](comparison.png)", "",
             "| Rows | Workload | Engine / mode | Mean ± SD (ms) | p50 | p95 | Runs |", "|---:|---|---|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['rows']:,} | {row['workload']} | {row['engine']} / {row['mode']} | {row['mean_ms']:.3f} ± {row['stddev_ms']:.3f} | {row['p50_ms']:.3f} | {row['p95_ms']:.3f} | {row['runs']} |")
    environment_records = environments(inputs, suite, profile)
    percentile = "nearest-rank" if suite == "sql" else "linear interpolation (NumPy quantile default)"
    lines.extend(["", *(f"- {note}" for note in notes), "", f"Statistics: sample standard deviation; p95 uses the historical {percentile} definition. Chart bars show mean ± SD; lower error bars are clipped to the positive plotting floor when necessary on the logarithmic axis.", "", "Recorded run dates:", ""])
    for record in environment_records:
        if "manifest" in record:
            lines.append(f"- {record['source']}: {record.get('started_at', 'start date unavailable')} → {record.get('completed_at', 'completion date unavailable')}; `{record['manifest']}`")
    lines.extend(["", "Recorded resource limits, platform, versions, source hashes and physical designs are saved in [environments.json](environments.json). Any deployment.json supplied with a run is preserved with its checksum, including observed container image/quota/version metadata. Missing metadata is unknown; no environment equality is assumed.", "", "Sources:", "", *(f"- {key}: `{value}`" for key, value in inputs.items())])
    (output / "report.md").write_text("\n".join(lines) + "\n")
    (output / "inputs.json").write_text(json.dumps(inputs, indent=2))
    (output / "environments.json").write_text(json.dumps(environment_records, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("sql", "frames"), required=True)
    parser.add_argument("--profile", choices=("baseline", "optimized"), default="baseline")
    parser.add_argument("--doris-dir", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    reference = args.reference_dir or (ROOT / "results" if args.suite == "sql" else ROOT / "results/pandas-duckdb/run-20260907T022808832856Z")
    output, doris, reference = args.output_dir.resolve(), args.doris_dir.resolve(), reference.resolve()
    if output.exists() or any(output == root or root.is_relative_to(output) for root in (reference, doris)):
        parser.error("output-dir must be a fresh directory and cannot contain the input directories")
    try:
        rows, notes = sql_comparison(reference, doris, args.profile) if args.suite == "sql" else frame_comparison(reference, doris)
    except (ValueError, KeyError, FileNotFoundError) as error:
        parser.error(str(error))
    write_comparison(output, rows, notes, args.suite, args.profile if args.suite == "sql" else "warm", {"reference_dir": str(reference), "doris_dir": str(doris)})
    print(f"Comparison saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
