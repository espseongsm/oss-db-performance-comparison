"""Reproducible static charts and a Markdown readout from benchmark records."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from benchmarks.frame_workloads import LABELS, WORKLOADS

COLORS = {"pandas": "#3268B2", "duckdb": "#C77C22"}


def render_results(folder: Path) -> None:
    raw = pd.read_csv(folder / "measurements.csv")
    summary = pd.read_csv(folder / "summary.csv")
    comparison = pd.read_csv(folder / "comparison.csv")
    manifest = json.loads((folder / "manifest.json").read_text())
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
    sizes = sorted(raw.rows.unique())
    count = manifest["settings"]["runs"]
    for mode in ("parquet", "memory"):
        fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
        for ax, workload in zip(axes.flat, WORKLOADS, strict=True):
            for offset, engine in ((-0.17, "pandas"), (0.17, "duckdb")):
                samples = [
                    raw.loc[
                        (raw["mode"] == mode)
                        & (raw.workload == workload)
                        & (raw.engine == engine)
                        & (raw.rows == rows),
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
                        "facecolor": COLORS[engine] if engine == "pandas" else "white",
                        "edgecolor": COLORS[engine],
                        "linewidth": 1.5,
                    },
                    medianprops={"color": "#252525", "linewidth": 1.5},
                    whiskerprops={"color": COLORS[engine]},
                    capprops={"color": COLORS[engine]},
                    flierprops={
                        "marker": "o",
                        "markersize": 3,
                        "markeredgecolor": COLORS[engine],
                    },
                    label=engine,
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
            f"{title}\n{count} measured runs per case | boxes: IQR, line: median, dots: outliers retained",
            fontsize=14,
        )
        fig.savefig(folder / f"timing-{mode}.png", dpi=160)
        plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(14, 7), layout="constrained")
    lower = comparison.ratio_ci95_low.fillna(comparison.pandas_over_duckdb).min()
    upper = comparison.ratio_ci95_high.fillna(comparison.pandas_over_duckdb).max()
    limits = (min(0.5, lower * 0.9), max(2, upper * 1.15))
    ticks = [
        2.0**power
        for power in range(
            int(np.floor(np.log2(limits[0]))), int(np.ceil(np.log2(limits[1]))) + 1
        )
        if limits[0] <= 2.0**power <= limits[1]
    ]
    for ax, mode in zip(axes, ("parquet", "memory"), strict=True):
        part = comparison.loc[comparison["mode"] == mode].sort_values(
            ["workload", "rows"]
        )
        positions = np.arange(len(part))
        ratios = part.pandas_over_duckdb.to_numpy()
        ax.scatter(ratios, positions, color=COLORS["pandas"], s=35, zorder=3)
        if part.ratio_ci95_low.notna().all():
            ax.hlines(
                positions,
                part.ratio_ci95_low,
                part.ratio_ci95_high,
                color=COLORS["pandas"],
                linewidth=1.5,
            )
        ax.axvline(1, color="#555555", linestyle="--", linewidth=1)
        labels = [f"{LABELS[row.workload]} / {row.rows:,}" for row in part.itertuples()]
        ax.set(
            yticks=positions,
            yticklabels=labels,
            xscale="log",
            title=mode,
            xlabel="pandas mean / DuckDB mean (log scale)",
        )
        ax.invert_yaxis()
        ax.set_xticks(ticks, labels=[f"{tick:g}×" for tick in ticks])
        ax.set_xlim(limits)
        ax.minorticks_off()
        ax.grid(axis="x", alpha=0.18)
    interval_note = (
        f"95% paired-block bootstrap intervals ({manifest['settings']['blocks']} blocks)"
        if manifest["settings"]["blocks"] > 1
        else "Intervals unavailable: only one measurement block"
    )
    fig.suptitle(
        "Relative elapsed time\nAbove 1: DuckDB faster | below 1: pandas faster\n"
        + interval_note,
        fontsize=14,
    )
    fig.savefig(folder / "speedup.png", dpi=160)
    plt.close(fig)
    write_report(folder, summary, comparison, manifest)


def write_report(
    folder: Path, summary: pd.DataFrame, comparison: pd.DataFrame, manifest: dict
) -> None:
    settings = manifest["settings"]
    text = [
        "# pandas vs DuckDB 전처리 실험",
        "",
        f"실행 완료(UTC): {manifest['completed_at']} / 상태: {manifest['status']}",
        "",
        f"입력 크기: {', '.join(f'{n:,}' for n in settings['sizes'])}행. "
        f"8개 수치형 컬럼. 각 조합 {settings['runs']}회, {settings['blocks']}개 묶음. "
        "아래 값은 생성 데이터에 대한 실제 측정이며 다른 데이터 유형에 일반화할 수 없다.",
        "",
        "## 평균 실행 시간",
        "",
        "±는 표본 표준편차다. 배율은 pandas 평균 / DuckDB 평균으로, 1보다 크면 DuckDB가 빠르다.",
        "",
        "| 입력 행 | 입력 방식 | 작업 | pandas 평균 ± SD(ms) | DuckDB 평균 ± SD(ms) | 배율 |",
        "|---:|---|---|---:|---:|---:|",
    ]
    indexed = summary.set_index(["rows", "mode", "workload", "engine"])
    for row in comparison.itertuples():
        p = indexed.loc[(row.rows, row.mode, row.workload, "pandas")]
        d = indexed.loc[(row.rows, row.mode, row.workload, "duckdb")]
        text.append(
            f"| {row.rows:,} | {row.mode} | {LABELS[row.workload]} | "
            f"{p.mean_ms:.3f} ± {p.std_ms:.3f} | {d.mean_ms:.3f} ± {d.std_ms:.3f} | "
            f"{row.pandas_over_duckdb:.2f}× |"
        )
    text += [
        "",
        "## 분포와 상대 시간",
        "",
        "![파일 입력 시간 분포](timing-parquet.png)",
        "",
        "![메모리 입력 시간 분포](timing-memory.png)",
        "",
        "![상대 실행 시간](speedup.png)",
        "",
        "## 측정 범위와 해석",
        "",
        "- parquet: 파일 읽기·변환·pandas 결과 생성 포함. pandas도 필요한 컬럼과 필터 pushdown을 사용한다.",
        "- memory: 필요한 원본 컬럼을 pandas로 미리 읽은 상태. DuckDB도 같은 DataFrame을 등록해 SQL을 수행한다. 사전 읽기·등록은 시간에서 제외한다.",
        "- DuckDB .df()의 전체 결과 생성 비용 포함. clean_derive는 입력과 같은 행 수를 반환하고 groupby는 최대 50행만 반환한다.",
        "- 금액은 정수 센트, 결측 할인율은 0, 할인 후 금액은 정수 내림. 출력 정렬은 요구하지 않는다.",
        "- 데이터 생성·import·연결·명시적 GC·결과 해시 검증은 시간에서 제외. 각 새 프로세스는 1회 워밍업 후 측정한다. 자동 GC는 켜져 있다.",
        "- 엔진은 한 번에 하나씩 별도 프로세스로 실행한다. 묶음마다 케이스와 엔진 순서를 시드로 섞는다. 두 엔진의 동일 묶음을 대응시킨다.",
        "- OS 파일 캐시를 비우지 않은 warm-cache 실험이다. 메모리 한도를 넘는 데이터, 문자열·UDF·전역 정렬·중복 제거는 이번 범위에 없다.",
        f"- DuckDB와 Arrow의 스레드 한도는 {settings['threads']}개, DuckDB 내부 메모리 한도는 {settings['memory_gb']}GB다. pandas의 모든 연산이 멀티스레드인 것은 아니다.",
        "- 평균 신뢰구간은 묶음 평균의 Student t 구간, 배율 구간은 대응 묶음 bootstrap 10,000회다. 기본 6개 묶음으로 추정한 탐색적 구간이며 시스템 부하·캐시·시간 의존성을 완전히 제거하지 못한다.",
        f"- p95/p99는 {settings['runs']}개 표본의 선형 보간 추정으로 꼬리 지연 보장이 아니다. Tukey 이상치는 개수만 표시하고 제거하지 않는다.",
        "- memory.csv의 process_peak_rss_bytes는 별도의 새 프로세스에서 1회 실행한 OS 최대 RSS다. import·입력 사전 로딩을 포함하고 검증은 제외한다. 30회 메모리 평균이나 연산만의 추가 메모리가 아니다.",
        "- 모든 측정 결과의 전체 행을 순서 독립 해시 합·XOR로 검증하고 엔진·입력 방식끼리 비교했다. 해시 충돌 가능성은 0이 아니며 단위 테스트에서는 실제 값을 직접 비교한다.",
        "- 데이터는 독립 균등분포의 수치형 합성 매출, 5% 할인 결측, 지역 50개, 계정 dimension 10만 행이다. 실제 업무 데이터의 편향·문자열·폭에 따라 결과가 달라진다.",
        "",
        "## 환경과 원자료",
        "",
        f"- {manifest['platform']} / Python {manifest['python'].split()[0]} / "
        f"메모리 {manifest['physical_memory_bytes'] / 2**30:.0f}GiB / 논리 CPU {manifest['cpu_logical_count']}",
        f"- 패키지: {manifest['versions']}",
        "- measurements.csv / measurements.jsonl: 전체 개별 시간·CPU 시간·실행 순번·검증 여부",
        "- summary.csv / summary.json: 평균·표본 SD·중앙값·최소·최대·Q1/Q3·IQR·MAD·p90/p95/p99·CV·95% CI·처리량",
        "- comparison.csv: 평균 배율·대응 묶음 bootstrap 구간",
        "- datasets.json / validation.json: 파일 SHA-256·스키마/크기·결과 fingerprint",
        "- manifest.json / execution-order.json: 버전·설정·소스 해시·순서·시스템 CPU/메모리/swap",
        "",
        "## 공식 API 근거",
        "",
        "- [DuckDB → pandas .df()](https://duckdb.org/docs/current/guides/python/export_pandas)",
        "- [DuckDB로 pandas 입력 질의](https://duckdb.org/docs/current/guides/python/sql_on_pandas)",
        "- [pandas read_parquet 컬럼·필터](https://pandas.pydata.org/docs/reference/api/pandas.read_parquet.html)",
    ]
    (folder / "report.md").write_text("\n".join(text) + "\n")


if __name__ == "__main__":
    render_results(Path(sys.argv[1]))
