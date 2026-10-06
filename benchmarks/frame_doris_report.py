"""Add Doris to the curated preprocessing report without rewriting its old results."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from benchmarks.frame_charts import COLORS
from benchmarks.frame_workloads import LABELS, WORKLOADS

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "results/pandas-duckdb/run-20260907T022808832856Z"
DORIS = ROOT / "results/doris/frames/run-20261006T000356880611Z"
COMPARISON = ROOT / "results/doris/comparisons/frames-20261006-final/comparison.csv"
BACKUP = ROOT / "results/doris/original-report-backup/frames"
PROTECTED = (
    "measurements.csv",
    "measurements.jsonl",
    "summary.csv",
    "summary.json",
    "comparison.csv",
    "manifest.json",
    "datasets.json",
    "validation.json",
    "execution-order.json",
    "memory.csv",
    "qa.json",
    "speedup.png",
)


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def checked_inputs(folder: Path, doris: Path, comparison: Path):
    manifest = json.loads((doris / "manifest.json").read_text())
    if manifest["status"] != "complete" or manifest["measurement_count"] != 360:
        raise ValueError("Doris full run must contain 360 completed measurements")
    for name, expected in manifest["reference_sha256"].items():
        if digest(folder / name) != expected:
            raise ValueError(f"Historical reference changed: {name}")
    old_data = {
        d["rows"]: d["sha256"]
        for d in json.loads((folder / "datasets.json").read_text())
    }
    new_data = {
        d["rows"]: d["sha256"]
        for d in json.loads((doris / "datasets.json").read_text())
    }
    if old_data != new_data:
        raise ValueError("Doris inputs differ from the historical Parquet files")
    if (folder / "validation.json").read_text() != (
        doris / "validation.json"
    ).read_text():
        old_validation = json.loads((folder / "validation.json").read_text())
        if old_validation != json.loads((doris / "validation.json").read_text()):
            raise ValueError("Doris full-result fingerprints differ")
    raw = pd.read_csv(folder / "measurements.csv")
    new_raw = pd.read_csv(doris / "measurements.csv")
    summary = pd.read_csv(doris / "summary.csv")
    verified = pd.read_csv(comparison)
    for frame, count, groups in ((raw, 1440, 48), (new_raw, 360, 12)):
        grouped = frame.groupby(["rows", "mode", "workload", "engine"])
        if (
            len(frame) != count
            or len(grouped) != groups
            or not grouped.size().eq(30).all()
        ):
            raise ValueError("Incomplete measurement matrix")
        if not frame.validated.eq(True).all():
            raise ValueError("Unvalidated measurements")
    indexed = summary.set_index(["rows", "workload"])
    for key, group in new_raw.groupby(["rows", "workload"]):
        row = indexed.loc[key]
        validated = verified.loc[
            (verified.engine == "doris")
            & (verified.rows == key[0])
            & (verified.workload == key[1])
        ]
        if len(validated) != 1 or row["n"] != 30 or row.blocks != 6:
            raise ValueError(f"Doris summary/comparison case mismatch: {key}")
        expected = [group.elapsed_ms.mean(), group.elapsed_ms.std(ddof=1)]
        for values in (
            [row.mean_ms, row.std_ms],
            validated[["mean_ms", "stddev_ms"]].iloc[0],
        ):
            if not np.allclose(values, expected, rtol=1e-12, atol=1e-8):
                raise ValueError(f"Doris summary does not match raw results: {key}")
    return raw, new_raw, indexed, manifest


def render_distributions(folder: Path, raw: pd.DataFrame, doris: pd.DataFrame) -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "text.color": "#252525",
            "axes.labelcolor": "#252525",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.axisbelow": True,
            "savefig.facecolor": "white",
        }
    )
    colors = {**COLORS, "doris": "#2B8C60"}
    sizes = sorted(raw.rows.unique())
    for mode in ("parquet", "memory"):
        fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
        for ax, workload in zip(axes.flat, WORKLOADS, strict=True):
            for offset, engine in ((-0.31, "pandas"), (0, "duckdb"), (0.31, "doris")):
                source = doris if engine == "doris" else raw.loc[raw["mode"] == mode]
                samples = [
                    source.loc[
                        (source.workload == workload)
                        & (source.engine == engine)
                        & (source.rows == rows),
                        "elapsed_ms",
                    ].to_numpy()
                    for rows in sizes
                ]
                ax.boxplot(
                    samples,
                    positions=np.arange(len(sizes)) + offset,
                    widths=0.27,
                    patch_artist=True,
                    manage_ticks=False,
                    boxprops={
                        "facecolor": "white" if engine == "duckdb" else colors[engine],
                        "edgecolor": colors[engine],
                        "linewidth": 1.5,
                    },
                    medianprops={"color": "#252525", "linewidth": 1.5},
                    whiskerprops={"color": colors[engine]},
                    capprops={"color": colors[engine]},
                    flierprops={
                        "marker": "o",
                        "markersize": 3,
                        "markeredgecolor": colors[engine],
                    },
                    label="doris (preloaded table)" if engine == "doris" else engine,
                )
            ax.set(
                title=LABELS[workload],
                ylabel="Elapsed time (ms, log scale)",
                xticks=np.arange(len(sizes)),
                xticklabels=[f"{x:,}" for x in sizes],
                xlabel="Input rows",
                yscale="log",
            )
            ax.grid(axis="y", alpha=0.18)
            ax.legend(loc="upper left", frameon=False)
        title = (
            "Parquet to pandas result"
            if mode == "parquet"
            else "In-memory data to pandas result"
        )
        fig.suptitle(
            f"{title}\n30 measured runs per case | boxes: IQR, line: median, dots: outliers retained\n"
            "Doris: same preloaded-table samples in both figures | 4 CPU / 8 GiB | SQL + full fetch",
            fontsize=14,
        )
        fig.savefig(folder / f"timing-{mode}.png", dpi=160)
        plt.close(fig)


def extend_report(folder: Path, doris: Path, comparison: Path) -> dict:
    raw, new_raw, summary, manifest = checked_inputs(folder, doris, comparison)
    protected = {name: digest(folder / name) for name in PROTECTED}
    BACKUP.mkdir(parents=True, exist_ok=True)
    for name in ("report.md", "timing-parquet.png", "timing-memory.png", "speedup.png"):
        if not (BACKUP / name).exists():
            shutil.copyfile(folder / name, BACKUP / name)
    body = (BACKUP / "report.md").read_text()
    original = body
    headings = re.findall(r"^#+ .+$", body, re.MULTILINE)
    lines, table_rows = [], 0
    workloads = {label: workload for workload, label in LABELS.items()}
    for line in body.splitlines():
        if line.startswith("| 입력 행 |"):
            line = line.replace(
                "| 배율 |", "| Doris 적재 테이블 평균 ± SD(ms) | 배율 |"
            )
        elif line.startswith("|---:"):
            cells = line.split("|")
            cells.insert(-2, "---:")
            line = "|".join(cells)
        else:
            match = re.match(r"^\| ([\d,]+) \| (memory|parquet) \| ([^|]+) \|", line)
            if match:
                rows = int(match[1].replace(",", ""))
                record = summary.loc[(rows, workloads[match[3].strip()])]
                cells = line.split("|")
                cells.insert(-2, f" {record.mean_ms:.3f} ± {record.std_ms:.3f} ")
                line = "|".join(cells)
                table_rows += 1
        lines.append(line)
    if table_rows != 24:
        raise ValueError("Expected exactly 24 historical comparison rows")
    body = "\n".join(lines) + "\n"
    body = body.replace(
        "## 결과 해석\n\n",
        "## 결과 해석\n\n"
        "아래 기존 해석과 다섯 항목의 배율은 로컬 pandas/DuckDB 두 경로의 비교다. "
        "Doris의 사전 적재 테이블 경로는 뒤의 추가 해석에서 따로 평가하며, "
        "집계·조인에서 DuckDB가 모든 경로보다 빠르다는 결론으로 확장하지 않는다.\n\n",
        1,
    )
    old_stats = raw.groupby(["rows", "mode", "workload", "engine"]).elapsed_ms.agg(
        ["mean", "std"]
    )
    largest = int(raw.rows.max())
    clean = summary.loc[(largest, "clean_derive")]
    filtered = summary.loc[(largest, "filter_project")]
    grouped = summary.loc[(largest, "groupby")]
    joined = summary.loc[(largest, "join_groupby")]
    parquet_clean = old_stats.loc[(largest, "parquet", "clean_derive", "duckdb")]
    parquet_filter = old_stats.loc[(largest, "parquet", "filter_project", "duckdb")]
    parquet_group = old_stats.loc[(largest, "parquet", "groupby", "duckdb")]
    memory_group = old_stats.loc[(largest, "memory", "groupby", "duckdb")]
    parquet_join = old_stats.loc[(largest, "parquet", "join_groupby", "duckdb")]
    group_difference = (grouped.mean_ms / parquet_group["mean"] - 1) * 100
    group_direction = "낮았다" if group_difference < 0 else "높았다"
    join_difference = abs(joined.mean_ms / parquet_join["mean"] - 1) * 100
    relative = Path("../../doris/frames") / doris.name
    note = (
        "**Doris 추가 비교(2026-10-06):** 기존 1,440회 결과에 Doris 12개 조건·360회 측정을 추가했다. "
        "Doris는 사전 적재 테이블에서 수행한 한 가지 입력 경로다. 아래 memory/parquet 행과 두 분포 그림에는 "
        "같은 Doris 측정값을 함께 표시하며, 24개 독립 조건을 새로 측정한 것이 아니다. "
        f"[Doris 원시 결과]({relative}/measurements.csv)의 입력 해시와 전체 출력 fingerprint가 기존 값과 일치했다.\n\n"
        f"- **많은 행을 Python으로 반환하는 작업:** {largest:,}행의 결측치·파생 컬럼 처리는 "
        f"Doris {clean.mean_ms / 1000:.3f}초, 필터·파생 컬럼은 {filtered.mean_ms / 1000:.3f}초였다. "
        f"DuckDB parquet 경로는 각각 {parquet_clean['mean']:.3f}ms, {parquet_filter['mean']:.3f}ms였다. "
        "이 조건에서는 로컬 전처리 경로를 우선 검토할 근거가 있다.\n"
        f"- **작은 결과를 반환하는 집계:** {largest:,}행의 지역별 집계는 Doris "
        f"{grouped.mean_ms:.3f}ms, DuckDB parquet {parquet_group['mean']:.3f}ms로 Doris의 관측 평균이 "
        f"{abs(group_difference):.1f}% {group_direction}. DuckDB memory 경로는 "
        f"{memory_group['mean']:.3f}ms였다. 시작 조건이 달라 어떤 경로의 데이터를 이미 보유했는지가 중요하다.\n"
        f"- **조인 후 집계의 작은 평균 차이:** {largest:,}행에서 Doris "
        f"{joined.mean_ms:.3f} ± {joined.std_ms:.3f}ms, DuckDB parquet "
        f"{parquet_join['mean']:.3f} ± {parquet_join['std']:.3f}ms였다. "
        f"평균 차이 {join_difference:.1f}%만으로 우위를 확정하지 않는다. "
        "Doris의 반복 변동성과 별도 실행 시점을 함께 고려해야 한다.\n\n"
        "이번 결과에서는 큰 결과를 Python으로 가져와 가공할 때 로컬 pandas/DuckDB 경로를 우선 검토한다. "
        "이미 서버에 데이터가 있고 작은 집계 결과만 반환한다면 Doris도 검토 후보다. "
        "Doris 시간은 SQL 요청·전체 결과 수신·DataFrame 생성·int64 정규화의 합계이며, "
        "각 비용을 분리해 측정하지 않아 시간 차이의 원인을 서버 실행이나 통신 비용으로 단정하지 않는다. "
        "9월과 10월 실행의 OS·패키지·배포 경로가 다르고 Doris를 포함한 대응 묶음 bootstrap도 없으므로, "
        "이 비교는 관측한 애플리케이션 경로의 선택 근거이며 동일 조건에서의 통계적 우열을 확정하지 않는다.\n\n"
    )
    body = body.replace("## 평균 실행 시간", note + "## 평균 실행 시간", 1)
    boundary = (
        "- Doris(warehouse): 사전 적재 테이블의 SQL 요청부터 전체 결과 수신·DataFrame 생성·int64 정규화까지 포함한다. "
        "연결·Parquet 적재·워밍업·결과 검증은 제외하며 적재 시간은 별도로 기록한다. memory/parquet의 "
        "서로 다른 시작 조건과 함께 비교하는 애플리케이션 경로 측정이다.\n"
        "- Doris는 4 CPU·8GiB Docker 환경·단일 클라이언트·warm-cache로 실행하고 SQL/query cache를 비활성화했다. "
        "Doris CPU 시간은 로컬 클라이언트 값이며 서버 메모리는 기존 memory.csv의 프로세스 RSS와 구분한다.\n"
        "- 상대 시간 그림과 배율 열의 대응 묶음 bootstrap 구간은 기존 pandas/DuckDB 실험에만 해당한다. "
        "Doris는 10월의 별도 실행이므로 과거 두 엔진과 대응하는 반복이 없으며 Doris 배율 구간은 만들지 않는다.\n"
    )
    body = body.replace("## 환경과 원자료", boundary + "\n## 환경과 원자료", 1)
    environment = (
        f"- Doris 추가 실행: {manifest['started_at']}–{manifest['completed_at']} (UTC), "
        f"{manifest['platform']}, Python {manifest['python'].split()[0]}, "
        f"Doris {manifest['doris']['version']}, pandas {manifest['versions']['pandas']}. "
        "기존 9월 실행과 측정일·OS·패키지·호스트 부하 및 배포 경로가 다르다.\n"
        f"- [doris-summary.csv](doris-summary.csv): 12개 warehouse 조건의 통계; "
        f"[Doris manifest]({relative}/manifest.json), [적재 기록]({relative}/doris-setup.json). "
        "기존 raw·summary·comparison·manifest와 speedup.png는 그대로 보존했다.\n"
    )
    body = body.replace("## 공식 API 근거", environment + "\n## 공식 API 근거", 1)
    if re.findall(r"^#+ .+$", body, re.MULTILINE) != headings:
        raise ValueError("Historical report headings/order changed")
    for paragraph in original.split("\n\n"):
        if not paragraph.startswith("| ") and paragraph not in body:
            raise ValueError("Historical report narrative changed")
    render_distributions(folder, raw, new_raw)
    shutil.copyfile(doris / "summary.csv", folder / "doris-summary.csv")
    (folder / "report.md").write_text(body)
    after = {name: digest(folder / name) for name in PROTECTED}
    if after != protected:
        raise ValueError(
            "Historical raw data/statistics/paired-bootstrap figure changed"
        )
    audit = {
        "doris_source": str(doris.relative_to(ROOT)),
        "verified_comparison": str(comparison.relative_to(ROOT)),
        "doris_requests": len(new_raw),
        "doris_cases": 12,
        "table_rows": table_rows,
        "same_doris_cases_reused_in_memory_and_parquet": True,
        "historical_headings_and_narrative_preserved": True,
        "protected_sha256": protected,
        "source_sha256": digest(Path(__file__)),
        "output_sha256": {
            name: digest(folder / name)
            for name in (
                "report.md",
                "timing-parquet.png",
                "timing-memory.png",
                "doris-summary.csv",
            )
        },
    }
    (folder / "doris-render-audit.json").write_text(json.dumps(audit, indent=2))
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-dir", type=Path, default=ORIGINAL)
    parser.add_argument("--doris-dir", type=Path, default=DORIS)
    parser.add_argument("--comparison-file", type=Path, default=COMPARISON)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            extend_report(
                args.reference_dir.resolve(),
                args.doris_dir.resolve(),
                args.comparison_file.resolve(),
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
