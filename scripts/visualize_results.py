# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.10.6"]
# ///
"""Render performance and storage charts from verified benchmark artifacts."""

import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
OUTPUT = RESULTS / "charts"
ENGINES = ("clickhouse", "duckdb", "postgres", "sqlite")
LABELS = ("ClickHouse", "DuckDB", "PostgreSQL", "SQLite")
QUERIES = ("small", "medium", "large", "join")
BLUE = "#3975B9"
GOLD = "#B88725"


def read(path):
    return json.loads(path.read_text())


def query_stats(profile, engine):
    folder = RESULTS / engine if profile == "baseline" else RESULTS / profile / engine
    assert read(folder / "dataset.json")["actual_rows"] == 1_000_000_000
    raw = [
        json.loads(line)
        for line in (folder / "measurements.jsonl").read_text().splitlines()
    ]
    reference = read(RESULTS / "clickhouse" / "summary.json")["queries"]
    stats = {}
    for query in QUERIES:
        records = [row for row in raw if row["query"] == query]
        if not records:
            assert (profile, engine, query) == ("optimized", "sqlite", "join")
            continue
        assert len(records) == 30
        assert {row["iteration"] for row in records} == set(range(1, 31))
        assert all(row["digest"] == reference[query]["digest"] for row in records)
        values = sorted(row["elapsed_ms"] for row in records)
        stats[query] = (
            statistics.mean(values),
            statistics.median(values),
            values[28],
            statistics.stdev(values),
        )
    if not (profile == "optimized" and engine == "sqlite"):
        summary = read(folder / "summary.json")
        assert summary["rows"] == 1_000_000_000
        for query, values in stats.items():
            for key, value in zip(
                ("average_ms", "p50_ms", "p95_ms", "stddev_ms"), values
            ):
                assert abs(summary["queries"][query][key] - value) < 0.00001
    return stats


def save(fig, name):
    fig.savefig(OUTPUT / f"{name}.png", dpi=180, facecolor="white")
    plt.close(fig)


def draw_sd(ax, x, mean, sd, floor):
    lower, upper = mean - sd, mean + sd
    ax.vlines(x, max(lower, floor), upper, color="#252525", linewidth=1.5, zorder=4)
    ax.plot(x, upper, marker="_", markersize=11, color="#252525", zorder=5)
    if lower >= floor:
        ax.plot(x, lower, marker="_", markersize=11, color="#252525", zorder=5)
    else:
        ax.plot(
            x,
            floor,
            marker="v",
            markersize=6,
            markerfacecolor="white",
            markeredgecolor="#252525",
            clip_on=False,
            zorder=5,
        )


def performance(profile):
    data = [query_stats(profile, engine) for engine in ENGINES]
    fig, axes = plt.subplots(2, 2, figsize=(14, 11))
    fig.subplots_adjust(
        left=0.09, right=0.98, top=0.78, bottom=0.17, hspace=0.53, wspace=0.24
    )
    fig.suptitle(
        f"{'Baseline' if profile == 'baseline' else 'Optimized'}"
        " 평균 응답 시간과 표준편차",
        x=0.04,
        ha="left",
        y=0.97,
        fontsize=23,
        weight="bold",
    )
    fig.text(
        0.04,
        0.922,
        "fact 10억 행 · dimension 100만 행 · 단일 클라이언트"
        " · 검증 후 반복 측정 · 쿼리별 30회",
        fontsize=12,
    )
    fig.text(
        0.04,
        0.879,
        "막대가 낮을수록 빠릅니다  ↓     진한 파란색 = 해당 쿼리에서 가장 빠른 엔진",
        fontsize=14,
        weight="bold",
        color=BLUE,
    )
    fig.text(
        0.04,
        0.846,
        "막대 = 평균   |   검은 오차막대 = 평균 ± 1 표본 표준편차 (SD)"
        "   |   숫자 = 평균 ± SD (ms)",
        fontsize=12,
    )
    descriptions = ("단일 ID 조회", "날짜 범위 집계", "전체 집계", "조인 후 집계")
    floor = 0.01
    for ax, query, description in zip(axes.flat, QUERIES, descriptions):
        ax.set_title(
            f"{query}  |  {description}", loc="left", weight="bold", fontsize=15, pad=14
        )
        ranked = sorted(
            (index for index, stats in enumerate(data) if query in stats),
            key=lambda index: data[index][query][0],
        )
        for x, stats in enumerate(data):
            if query not in stats:
                ax.text(
                    x,
                    0.10,
                    "측정 중단\n6시간 초과\n순위 제외 (0/30)",
                    transform=ax.get_xaxis_transform(),
                    ha="center",
                    va="bottom",
                    color="#666666",
                    fontsize=11,
                )
                continue
            mean, _, _, sd = stats[query]
            rank = ranked.index(x) + 1
            ax.bar(
                x,
                mean - floor,
                bottom=floor,
                width=0.56,
                color=BLUE if rank == 1 else "#DCE8F5",
                edgecolor="#285586",
                linewidth=0.8,
                zorder=3,
            )
            draw_sd(ax, x, mean, sd, floor)
            value = f"{mean:,.3f}" if mean < 1000 else f"{mean:,.0f}"
            spread = f"{sd:,.3f}" if mean < 1000 else f"{sd:,.0f}"
            ax.annotate(
                f"{'1위 · 가장 빠름' if rank == 1 else f'{rank}위'}"
                f"\n{value}\n± {spread} ms",
                (x, mean + sd),
                xytext=(0, 6),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=10,
                color="#285586" if rank == 1 else "#252525",
                weight="bold" if rank == 1 else "normal",
            )
        ax.set_yscale("log")
        ax.set_ylim(floor, 10_000_000)
        ax.set_yticks([0.01, 1, 100, 10_000, 1_000_000])
        ax.yaxis.set_major_formatter(
            FuncFormatter(
                lambda value, _: f"{value:,.2f}" if value < 1 else f"{value:,.0f}"
            )
        )
        ax.set_xticks(range(4), LABELS)
        ax.set_xlim(-0.6, 3.6)
        ax.set_ylabel("평균 응답 시간 (ms · 로그 축)", fontsize=11)
        ax.grid(axis="y", color="#E5E5E5", zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(axis="x", length=0, pad=10)
    fig.text(
        0.04,
        0.035,
        "출처: measurements.jsonl · n=30, 표본 SD (분모 n-1)"
        " · 완주한 실행은 summary.json과 대조\n"
        "SD는 반복 측정값의 변동성입니다."
        " 평균의 신뢰구간이나 최솟값·최댓값을 의미하지 않습니다.\n"
        "▽: 평균-SD가 0.01 ms보다 작아 하한을 생략했습니다."
        " 이 표시는 측정된 최솟값이 아닙니다.\n"
        "동일한 로그 축: ±SD도 위아래 길이가 다릅니다."
        " 막대 높이의 비율은 시간 비율이 아닙니다. p50/p95는 표 참조.",
        fontsize=11,
        color="#555555",
    )
    save(fig, profile)


def comparison():
    data = {
        profile: [query_stats(profile, engine) for engine in ENGINES]
        for profile in ("baseline", "optimized")
    }
    fig, axes = plt.subplots(2, 2, figsize=(14, 10.5))
    fig.subplots_adjust(
        left=0.09, right=0.98, top=0.78, bottom=0.17, hspace=0.48, wspace=0.24
    )
    fig.suptitle(
        "Baseline과 Optimized 비교",
        x=0.04,
        ha="left",
        y=0.97,
        fontsize=23,
        weight="bold",
    )
    fig.text(
        0.04,
        0.92,
        "fact 10억 행 · dimension 100만 행 · 단일 클라이언트"
        " · 검증 후 반복 측정 · 각 프로파일 30회",
        fontsize=12,
    )
    fig.text(
        0.04,
        0.88,
        "막대가 낮을수록 빠름 ↓   |   오차막대: 평균 ± 1 표본 SD"
        "   |   위의 숫자: 최적화 속도 배수",
        fontsize=12,
    )
    fig.legend(
        handles=[
            Patch(facecolor=BLUE, edgecolor="#333333", label="baseline"),
            Patch(facecolor=GOLD, edgecolor="#333333", hatch="//", label="optimized"),
        ],
        loc="upper left",
        bbox_to_anchor=(0.035, 0.858),
        ncol=2,
        frameon=False,
    )
    floor = 0.01
    for ax, query in zip(axes.flat, QUERIES):
        ax.set_title(query, loc="left", fontsize=15, weight="bold", pad=14)
        for x, engine in enumerate(ENGINES):
            upper = 0
            for profile, offset, color, hatch in (
                ("baseline", -0.19, BLUE, None),
                ("optimized", 0.19, GOLD, "//"),
            ):
                stats = data[profile][x].get(query)
                if stats is None:
                    ax.text(
                        x + offset,
                        0.04,
                        "중단",
                        transform=ax.get_xaxis_transform(),
                        ha="center",
                        color="#666666",
                        fontsize=10,
                    )
                    continue
                mean, _, _, sd = stats
                ax.bar(
                    x + offset,
                    mean - floor,
                    bottom=floor,
                    width=0.32,
                    color=color,
                    edgecolor="#333333",
                    linewidth=0.6,
                    hatch=hatch,
                    zorder=3,
                )
                draw_sd(ax, x + offset, mean, sd, floor)
                upper = max(upper, mean + sd)
            optimized = data["optimized"][x].get(query)
            ratio = data["baseline"][x][query][0] / optimized[0] if optimized else None
            label = f"{ratio:,.2f}×" if ratio is not None else "비교 불가"
            ax.annotate(
                label,
                (x, upper),
                xytext=(0, 9),
                textcoords="offset points",
                ha="center",
                fontsize=11,
                weight="bold",
            )
        ax.set_yscale("log")
        ax.set_ylim(floor, 10_000_000)
        ax.set_yticks([0.01, 1, 100, 10_000, 1_000_000])
        ax.yaxis.set_major_formatter(
            FuncFormatter(
                lambda value, _: f"{value:,.2f}" if value < 1 else f"{value:,.0f}"
            )
        )
        ax.set_xticks(range(4), LABELS)
        ax.set_xlim(-0.6, 3.6)
        ax.set_ylabel("평균 응답 시간 (ms · 로그 축)", fontsize=11)
        ax.grid(axis="y", color="#E5E5E5", zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(axis="x", length=0, pad=10)
    fig.text(
        0.04,
        0.035,
        "속도 배수 = baseline 평균 ÷ optimized 평균."
        " 1보다 크면 개선, 1보다 작으면 악화입니다.\n"
        "출처: measurements.jsonl · n=30 · SD는 반복 간 변동성으로 신뢰구간이 아닙니다."
        " 정확한 평균 ± SD는 표 참조.\n"
        "동일한 로그 축, 시작값 0.01 ms. ▽는 평균-SD가 표시 범위 아래임을 뜻하며"
        " 관측 최솟값이 아닙니다.\n"
        "막대 높이의 비율은 시간 비율이 아닙니다. SQLite optimized join은 0/30이므로"
        " 속도 배수를 계산하지 않았습니다.",
        fontsize=11,
        color="#555555",
    )
    save(fig, "comparison")


def storage():
    fig, ax = plt.subplots(figsize=(13, 5.6))
    fig.subplots_adjust(left=0.14, right=0.95, top=0.73, bottom=0.2)
    fig.suptitle(
        "엔진별 저장 크기", x=0.04, ha="left", y=0.97, fontsize=21, weight="bold"
    )
    fig.text(
        0.04,
        0.885,
        "fact 10억 행 · dimension 100만 행 · volume 삭제 직전 기록"
        " · GiB (작을수록 적은 공간)",
        fontsize=12,
    )
    for profile, offset, color, hatch in (
        ("baseline", -0.18, BLUE, None),
        ("optimized", 0.18, GOLD, "//"),
    ):
        for y, engine in enumerate(ENGINES):
            folder = (
                RESULTS / engine
                if profile == "baseline"
                else RESULTS / profile / engine
            )
            record = read(folder / "storage.json")
            if record["rows"] != 1_000_000_000:
                ax.text(
                    2,
                    y + offset,
                    "미확정 (파일럿 값 제외)",
                    va="center",
                    fontsize=10,
                    color="#666666",
                )
                continue
            value = record["total_bytes"] / 2**30
            assert round(value, 2) == round(record["total_gib"], 2)
            ax.barh(
                y + offset,
                value,
                height=0.29,
                color=color,
                edgecolor="#333333",
                linewidth=0.5,
                hatch=hatch,
                label=profile if y == 0 else None,
            )
            ax.text(value + 2, y + offset, f"{value:.2f}", va="center", fontsize=11)
    ax.set_yticks(range(4), LABELS)
    ax.set_ylim(3.55, -0.55)
    ax.set_xlim(0, 245)
    ax.set_xlabel("저장 크기 (GiB)")
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#E5E5E5")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.02), ncol=2, frameon=False)
    fig.text(
        0.04,
        0.045,
        "출처: 각 엔진 storage.json · PostgreSQL optimized:"
        " 현재 10억 행 파일의 212.76 GiB 반영\n"
        "SQLite optimized의 중단 당시 관측값(약 107.40 GiB)은"
        " 동일한 최종 산출물이 없어 차트에서 제외했습니다.",
        fontsize=10,
        color="#555555",
    )
    save(fig, "storage")


def main():
    font = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=font).get_name()
    plt.rcParams.update(
        {"font.size": 11, "axes.unicode_minus": False, "text.color": "#252525"}
    )
    OUTPUT.mkdir(exist_ok=True)
    for profile in ("baseline", "optimized"):
        performance(profile)
    comparison()
    storage()


if __name__ == "__main__":
    main()
