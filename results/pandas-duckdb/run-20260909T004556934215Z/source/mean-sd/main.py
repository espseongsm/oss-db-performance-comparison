"""Render this run in the previous report's vertical mean ± sample-SD style.

Chart contract: three static PNGs, one per input size, with four workload facets
and five execution paths per facet. Zero-based linear axes preserve magnitude;
exact numeric labels keep small local timings readable beside remote timings.
Blue, orange and olive plus hatching distinguish engines without color alone.
Negative SD lower bounds are clipped at zero and explicitly marked.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator, StrMethodFormatter

FOLDER = Path(__file__).resolve().parents[2]
WORKLOADS = [
    ("clean_derive", "결측치 처리 · 파생 컬럼"),
    ("filter_project", "필터 · 파생 컬럼"),
    ("groupby", "지역별 집계"),
    ("join_groupby", "조인 후 집계"),
]
CASES = [
    ("memory", "pandas", "메모리 · pandas"),
    ("memory", "duckdb", "메모리 · DuckDB"),
    ("parquet", "pandas", "파일 · pandas"),
    ("parquet", "duckdb", "파일 · DuckDB"),
    ("warehouse", "snowflake", "원격 · Snowflake"),
]
COLORS = {"pandas": "#3268B2", "duckdb": "#C77C22", "snowflake": "#73763B"}
HATCHES = {"pandas": None, "duckdb": "///", "snowflake": "..."}


def draw(summary, size, manifest):
    data = summary.loc[summary.rows == size].set_index(["workload", "mode", "engine"])
    fig, axes = plt.subplots(2, 2, figsize=(16, 12.6))
    fig.subplots_adjust(
        left=0.075, right=0.985, top=0.80, bottom=0.17, hspace=0.55, wspace=0.28
    )
    positions = [0.0, 1.0, 2.5, 3.5, 5.2]
    for ax, (work, title) in zip(axes.flat, WORKLOADS, strict=True):
        observations = [data.loc[(work, mode, engine)] for mode, engine, _ in CASES]
        maximum = max(row.mean_ms + row.std_ms for row in observations)
        for x, (_, engine, _), row in zip(positions, CASES, observations, strict=True):
            mean, sd = row.mean_ms, row.std_ms
            ax.bar(
                x,
                mean,
                width=0.58,
                color=COLORS[engine],
                edgecolor=COLORS[engine],
                linewidth=1.0,
                hatch=HATCHES[engine],
                alpha=1 if engine == "pandas" else 0.78,
            )
            ax.errorbar(
                x,
                mean,
                yerr=[[min(sd, mean)], [sd]],
                fmt="none",
                color="#25282D",
                capsize=4,
                capthick=1.2,
                elinewidth=1.2,
            )
            clipped = sd > mean
            if clipped:
                ax.plot(x, 0, marker="v", color="#25282D", markersize=6, clip_on=False)
            ax.text(
                x,
                mean + sd + maximum * 0.04,
                f"{mean:,.2f}\n± {sd:,.2f}" + (" *" if clipped else ""),
                ha="center",
                va="bottom",
                fontsize=13,
            )
        labels = [label.replace(" · ", "\n") for _, _, label in CASES]
        ax.set_xticks(positions, labels, fontsize=13)
        ax.set_xlim(-0.65, 5.95)
        ax.set_ylim(0, maximum * 1.33)
        ax.yaxis.set_major_locator(MaxNLocator(4, min_n_ticks=3))
        ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
        ax.tick_params(axis="both", length=0, pad=7, labelsize=13)
        ax.set_title(title, loc="left", pad=16, fontsize=21)
        ax.set_ylabel("실행 시간 (ms)", fontsize=14, labelpad=9)
        ax.grid(axis="y", color="#DFE2E6", linewidth=0.8)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#B8BDC5")
    size_label = {100_000: "10만", 1_000_000: "100만", 10_000_000: "1,000만"}[size]
    fig.text(0.045, 0.95, "pandas · DuckDB · Snowflake 전처리 성능", fontsize=28)
    fig.text(
        0.045,
        0.91,
        f"{size_label} 행  |  2026-09-09 실측  |  조건별 30회  |  낮을수록 빠름",
        fontsize=18,
    )
    fig.text(
        0.045,
        0.873,
        "막대 = 평균   ·   오차막대와 숫자 = 평균 ± 표본 표준편차 (SD)",
        fontsize=17,
    )
    warehouse = manifest["snowflake"]["warehouse_settings"]["size"]
    notes = [
        "메모리: 사전 로딩 제외  ·  파일: Parquet 읽기 포함  ·  두 경로 모두 로컬 Mac에서 실행",
        f"원격: Snowflake {warehouse} 적재 테이블 → SQL → 결과 다운로드·정규화  ·  업로드·적재 제외",
        "작업별 세로축 범위가 다름  ·  모든 축은 0부터 시작  ·  *와 ▼: SD 하한이 음수여서 0에서 잘림",
    ]
    for y, note in zip((0.095, 0.064, 0.033), notes, strict=True):
        fig.text(0.045, y, note, fontsize=14, color="#50555D")
    target = FOLDER / f"mean-sd-{size}.png"
    fig.savefig(target, dpi=160, facecolor="white")
    plt.close(fig)
    return target.name


def main():
    font_manager.fontManager.addfont("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    plt.rcParams.update(
        {
            "font.family": "Apple SD Gothic Neo",
            "text.color": "#25282D",
            "axes.labelcolor": "#25282D",
            "axes.unicode_minus": False,
        }
    )
    summary = pd.read_csv(FOLDER / "summary.csv")
    raw = pd.read_json(FOLDER / "measurements.jsonl", lines=True)
    keys = ["rows", "mode", "workload", "engine"]
    stats = raw.groupby(keys).elapsed_ms.agg(["mean", "std", "count"])
    saved = summary.set_index(keys).reindex(stats.index)
    assert len(stats) == 60 and len(raw) == 1800 and stats["count"].eq(30).all()
    assert np.allclose(saved.mean_ms, stats["mean"], rtol=1e-12)
    assert np.allclose(saved.std_ms, stats["std"], rtol=1e-12)
    manifest = json.loads((FOLDER / "manifest.json").read_text())
    paths = [draw(summary, size, manifest) for size in sorted(summary.rows.unique())]
    print(json.dumps({"figures": paths, "verified_mean_sd_pairs": len(stats)}))


if __name__ == "__main__":
    main()
