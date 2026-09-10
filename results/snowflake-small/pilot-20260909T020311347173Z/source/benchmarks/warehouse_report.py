"""Audit and report the benchmark performed inside a Small warehouse."""

import hashlib
import itertools
import json
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator, StrMethodFormatter

from benchmarks.frame_workloads import WORKLOADS

ENGINES = ("pandas", "duckdb", "snowflake")
NAMES = {"pandas": "pandas", "duckdb": "DuckDB", "snowflake": "Snowflake SQL"}
TITLES = {
    "clean_derive": "결측치 처리 · 파생 컬럼",
    "filter_project": "필터 · 파생 컬럼",
    "groupby": "지역별 집계",
    "join_groupby": "조인 후 집계",
}
COLORS = ("#3268B2", "#C77C22", "#73763B")
HATCHES = (None, "///", "...")
KEYS = ["rows", "workload", "engine"]


def audit(raw, manifest, references):
    settings = manifest["settings"]
    expected = set(itertools.product(settings["sizes"], WORKLOADS, ENGINES))
    groups = raw.groupby(KEYS)
    if set(groups.groups) != expected:
        raise ValueError("Incomplete or unexpected condition matrix")
    if raw.duplicated([*KEYS, "run"]).any():
        raise ValueError("Duplicate measurements")
    stats = []
    for key, group in groups:
        if sorted(group.run) != list(range(1, settings["runs"] + 1)):
            raise ValueError(f"Incorrect run count: {key}")
        expected_blocks = [
            (r - 1) // (settings["runs"] // settings["blocks"]) + 1 for r in group.run
        ]
        if list(group.block) != expected_blocks:
            raise ValueError(f"Incorrect block assignment: {key}")
        for row in group.to_dict("records"):
            if (
                not row["validated"]
                or row["fingerprint"] != references[f"{row['rows']}/{row['workload']}"]
            ):
                raise ValueError(f"Result mismatch: {key}")
            if row["output_rows"] != row["fingerprint"]["rows"]:
                raise ValueError(f"Output row count mismatch: {key}")
            components = row["setup_ms"] + row["compute_to_dataframe_ms"]
            if row["engine"] == "snowflake":
                if not pd.isna(row["input_ms"]):
                    raise ValueError("Native SQL input time must be null (fused scan)")
            else:
                components += row["input_ms"]
            if not math.isclose(row["elapsed_ms"], components, rel_tol=1e-9):
                raise ValueError(f"Timing components do not sum: {key}")
            if not math.isfinite(row["elapsed_ms"]) or row["elapsed_ms"] <= 0:
                raise ValueError(f"Invalid time: {key}")
        values = group.elapsed_ms.tolist()
        stats.append(
            dict(
                zip(KEYS, key, strict=True),
                n=len(values),
                mean_ms=statistics.mean(values),
                std_ms=statistics.stdev(values) if len(values) > 1 else None,
                median_ms=statistics.median(values),
                min_ms=min(values),
                max_ms=max(values),
                input_mean_ms=(
                    None if key[2] == "snowflake" else statistics.mean(group.input_ms)
                ),
                setup_mean_ms=statistics.mean(group.setup_ms),
                compute_mean_ms=statistics.mean(group.compute_to_dataframe_ms),
            )
        )
    summary = pd.DataFrame(stats)
    # Cross-check sample SD with an independent implementation.
    alternate = groups.elapsed_ms.agg(["mean", "std"])
    for row in summary.to_dict("records"):
        other = alternate.loc[tuple(row[k] for k in KEYS)]
        for saved, calculated in (
            (row["mean_ms"], other["mean"]),
            (row["std_ms"], other["std"]),
        ):
            if pd.isna(saved) and pd.isna(calculated):
                continue
            if not math.isclose(saved, calculated, rel_tol=1e-12, abs_tol=1e-9):
                raise ValueError("Independent statistics check failed")
    return summary


def draw(folder, summary, size, manifest):
    font = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = "Apple SD Gothic Neo"
    plt.rcParams["axes.unicode_minus"] = False
    data = summary.loc[summary.rows == size].set_index(["workload", "engine"])
    fig, axes = plt.subplots(2, 2, figsize=(14, 11.5))
    fig.subplots_adjust(
        left=0.09, right=0.98, top=0.79, bottom=0.17, hspace=0.51, wspace=0.27
    )
    for ax, (work, title) in zip(axes.flat, TITLES.items(), strict=True):
        records = [data.loc[(work, engine)] for engine in ENGINES]
        maximum = max(
            row.mean_ms + (0 if pd.isna(row.std_ms) else row.std_ms) for row in records
        )
        for x, (engine, color, hatch, row) in enumerate(
            zip(ENGINES, COLORS, HATCHES, records, strict=True)
        ):
            mean, sd = row.mean_ms, row.std_ms
            ax.bar(
                x,
                mean,
                width=0.53,
                color=color,
                edgecolor=color,
                hatch=hatch,
                alpha=0.85,
            )
            if pd.notna(sd):
                ax.errorbar(
                    x,
                    mean,
                    yerr=[[min(sd, mean)], [sd]],
                    fmt="none",
                    color="#25282D",
                    capsize=5,
                )
            clipped = pd.notna(sd) and sd > mean
            if clipped:
                ax.plot(x, 0, marker="v", color="#25282D", clip_on=False)
            label = (
                f"{mean:,.2f}\n± {sd:,.2f}" if pd.notna(sd) else f"{mean:,.2f}\nSD 미정"
            )
            ax.text(
                x,
                mean + (0 if pd.isna(sd) else sd) + maximum * 0.04,
                label + (" *" if clipped else ""),
                ha="center",
                va="bottom",
                fontsize=14,
            )
        ax.set_xticks(range(3), [NAMES[e] for e in ENGINES], fontsize=14)
        ax.set_ylim(0, maximum * 1.33)
        ax.set_xlim(-0.6, 2.6)
        ax.set_ylabel("실행 시간 (ms)", fontsize=14)
        ax.set_title(title, loc="left", fontsize=21, pad=18)
        ax.yaxis.set_major_locator(MaxNLocator(4, min_n_ticks=3))
        ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
        ax.tick_params(length=0, pad=8, labelsize=13)
        ax.grid(axis="y", color="#DFE2E6")
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#B8BDC5")
    settings = manifest["settings"]
    phase = "파일럿" if settings["pilot"] else "본 실험"
    fig.text(0.045, 0.95, "같은 Snowflake Small에서 전처리 성능 비교", fontsize=27)
    fig.text(
        0.045,
        0.907,
        f"{size:,} 행  |  {phase} · 조건별 {settings['runs']}회  |  낮을수록 빠름",
        fontsize=18,
    )
    fig.text(
        0.045,
        0.867,
        "막대 = 평균   ·   오차막대와 숫자 = 평균 ± 표본 표준편차 (SD)",
        fontsize=17,
    )
    notes = [
        "공통 Snowflake 테이블 → 처리 → 서버 내부 pandas DataFrame 완성까지 측정",
        "Python 런타임과 SQL 실행 엔진의 자원 배분은 다름  ·  Mac 다운로드·CALL 시작 시간 제외",
        "작업별 세로축 범위가 다름  ·  모든 축은 0부터 시작  ·  *와 ▼: SD 하한이 0에서 잘림",
    ]
    for y, note in zip((0.1, 0.066, 0.032), notes, strict=True):
        fig.text(0.045, y, note, fontsize=13, color="#50555D")
    name = f"mean-sd-{size}.png"
    fig.savefig(folder / name, dpi=160, facecolor="white")
    plt.close(fig)
    return name


def report_text(summary, manifest, calls, qa, images):
    settings = manifest["settings"]
    phase = "파일럿" if settings["pilot"] else "본 실험"
    largest = max(settings["sizes"])
    data = summary.set_index(KEYS)
    findings = []
    for work, title in TITLES.items():
        ranked = summary.loc[
            (summary.rows == largest) & (summary.workload == work)
        ].sort_values("mean_ms")
        best, next_best = ranked.iloc[0], ranked.iloc[1]
        findings.append(
            f"- {title}: {NAMES[best.engine]} 평균 {best.mean_ms:,.2f} ms, 다음 경로 {NAMES[next_best.engine]} 대비 {next_best.mean_ms / best.mean_ms:.2f}배 짧은 시간."
        )
    lines = [
        f"# 같은 Snowflake Small에서 pandas · DuckDB · SQL 비교 — {phase}",
        "",
        f"{largest:,}행에서 관측한 평균 기준 결과다. 같은 warehouse의 서로 다른 실행 경로를 비교하며, 아래 순위는 유의성 검정 결과가 아니다.",
        "",
        *findings,
        "",
        f"총 **{qa['measurements']:,}회**, **{qa['conditions']}개 조건 × {settings['runs']}회**를 완료했다. 모든 반복의 전체 결과 fingerprint가 공통 기준값과 일치한다. "
        + (
            "파일럿은 기능 검증이며 엔진 선택의 최종 근거로 사용하지 않는다."
            if settings["pilot"]
            else "평균 ± 표본 표준편차(SD, n−1)는 반복 간 변동성을 나타내며 신뢰구간이 아니다."
        ),
        "",
        "## 평균과 표준편차",
        "",
        "같은 색과 무늬를 유지한 세로 막대다. 모든 세로축은 0부터 시작하지만 작업별 범위는 다르다. 음수인 SD 하한은 0에서 자르고 표시한다.",
        "",
    ]
    for size, image in zip(sorted(settings["sizes"]), images, strict=True):
        lines.extend(
            [
                f"### {size:,}행",
                "",
                f"![{size:,}행 평균 ± 표본 SD]({image})",
                "",
                "| 작업 | pandas (ms) | DuckDB (ms) | Snowflake SQL (ms) |",
                "|---|---:|---:|---:|",
            ]
        )
        for work, title in TITLES.items():
            cells = []
            for engine in ENGINES:
                row = data.loc[(size, work, engine)]
                cells.append(
                    f"{row.mean_ms:,.2f} ± {row.std_ms:,.2f}"
                    if pd.notna(row.std_ms)
                    else f"{row.mean_ms:,.2f} (SD 미정)"
                )
            lines.append("| " + " | ".join([title, *cells]) + " |")
        lines.append("")
    runtime = calls[0]["runtime"]
    wh = manifest["snowflake"]["warehouse_settings"]
    lines.extend(
        [
            "## 측정 경계와 환경",
            "",
            "```mermaid",
            "flowchart LR",
            '  M["Mac: 적재·호출·지표 저장"] --> T["Small: 공통 Snowflake 테이블"]',
            '  T --> P["Python 프로시저: 읽기 → 일반 pandas"]',
            '  T --> D["Python 프로시저: 읽기 → DuckDB"]',
            '  T --> S["Snowflake SQL: 스캔·변환"]',
            '  P --> F["서버 내부 전체 pandas DataFrame"]',
            "  D --> F",
            "  S --> F",
            '  F --> V["시간 측정 종료 → 결과 검증"]',
            '  V --> R["Mac: 지표·검증 요약 수신"]',
            "```",
            "",
            "| 항목 | 설정 |",
            "|---|---|",
            f"| 실행 시각 (UTC) | {manifest['started_at']} ~ {manifest['completed_at']} |",
            f"| warehouse | {settings['warehouse']} / {wh['size']} / {wh['type']} |",
            f"| 반복 설계 | {settings['blocks']}묶음 × 조건당 {settings['runs'] // settings['blocks']}회; 각 CALL의 검증된 예열 1회 제외; 묶음 내 조건 순서 무작위 |",
            "| 공통 시작·종료 | 이미 적재된 동일 테이블 → Python 프로시저 내부의 전체 pandas DataFrame |",
            "| pandas·DuckDB 입력 | 필요한 컬럼 SELECT와 DataFrame 읽기·형 변환 포함. 필터 작업은 같은 조건을 두 경로 모두 SQL에 pushdown |",
            "| SQL 입력 | SQL에서 스캔·연산을 함께 수행. 별도 input_ms는 측정 불가하여 null |",
            "| 제외 | 최초 업로드/COPY, 연결, 프로시저 시작·CALL 왕복, 예열, GC, 검증 해시, query history 조회, 연결 해제 |",
            f"| 결과 캐시 | USE_CACHED_RESULT={runtime['use_cached_result']}; 데이터·메타데이터 캐시는 초기화하지 않음 |",
            f"| DuckDB | {settings['threads']} threads / memory_limit={settings['memory_gb']}GB; 연결·등록 시간 포함 |",
            f"| Python 런타임 | {runtime['platform']}; Python {runtime['python'].splitlines()[0]} |",
            f"| 런타임에서 보이는 자원 | CPU {runtime['visible_cpu_count']}개, RAM {runtime['visible_memory_bytes'] / 2**30:.1f} GiB; 엔진별 독점 할당량을 의미하지 않음 |",
            f"| 패키지 | {', '.join(f'{k} {v}' for k, v in runtime['versions'].items())} |",
            "",
            "## 해석 범위",
            "",
            "실행 위치와 warehouse 크기는 맞췄다. 일반 pandas와 DuckDB는 Python 프로시저의 메모리에서 계산하고, Snowflake SQL은 warehouse의 SQL 실행 엔진에서 계산한다. 따라서 같은 CPU·RAM을 독점하는 엔진 실험은 아니다. pandas API를 SQL로 번역하는 Snowpark pandas도 사용하지 않았다.",
            "",
            "pandas·DuckDB의 총 시간에는 원본 컬럼을 Python으로 읽는 비용이 포함된다. 집계 결과만 가져오는 SQL은 데이터 이동량 자체를 줄일 수 있다. 이 차이는 Snowflake 안에서 데이터를 처리하는 경로 선택에는 유용하지만 순수 계산 속도 차이로 해석하면 안 된다. 입력·설정·연산 시간은 summary.csv의 input_mean_ms / setup_mean_ms / compute_mean_ms에서 따로 확인한다.",
            "",
            "이전 실험은 로컬 Mac의 pandas·DuckDB와 원격 X-Small SQL의 Mac 다운로드까지 비교했다. 이번 실험은 실행 환경과 시간 측정 경계가 모두 달라서 이전 수치와 직접 개선 배율을 계산하지 않는다. 단일 요청, 합성 수치형 데이터, 선택한 크기·4개 작업의 결과이며 동시성·비용 효율·전체 제품 성능의 순위가 아니다.",
            "",
            "## 검증과 재현",
            "",
            f"- {qa['measurements']:,}개 fingerprint·반복 번호·묶음·시간 합계 검증, {qa['conditions']}쌍 평균/SD를 statistics와 pandas로 교차 확인.",
            f"- 측정에 연결된 child query history: {qa['history_found']:,}/{qa['history_expected']:,}개 확보. 누락 {qa['history_missing']:,}개는 서버 세부 시간의 미확보이며 직접 측정 시간과 구분한다.",
            "- 런타임 버전·warehouse·결과 캐시 설정을 모든 CALL에서 확인. 소스 사본 SHA-256을 검증했다.",
            "- [원시 측정](measurements.jsonl), [요약 CSV](summary.csv), [실행 명세](manifest.json), [호출·런타임](calls.json), [QA](qa.json), [실행한 프로시저](procedure.py).",
            "- 실행: `uv run --no-sync python main.py warehouse-frames` (기능 검증은 `--pilot`). macOS Keychain의 benchmark 비밀번호 사용.",
            "",
            "Snowflake 공식 문서: [Python 프로시저의 단일 노드 실행](https://docs.snowflake.com/en/developer-guide/snowpark/python/python-snowpark-training-ml), [Python 프로시저 제약](https://docs.snowflake.com/en/developer-guide/stored-procedure/python/procedure-python-limitations), [Snowpark pandas](https://docs.snowflake.com/en/developer-guide/snowpark/python/pandas-on-snowflake).",
            "",
        ]
    )
    return "\n".join(lines)


def render_results(folder, raw, manifest):
    references = json.loads((folder / "validation.json").read_text())
    summary = audit(raw, manifest, references)
    calls = json.loads((folder / "calls.json").read_text())
    for call in calls:
        runtime = call["runtime"]
        if (
            runtime["versions"] != manifest["packages"]
            or runtime["warehouse"].strip('"') != manifest["settings"]["warehouse"]
            or runtime["use_cached_result"].lower() != "false"
        ):
            raise ValueError("Unexpected runtime, warehouse, or result-cache setting")
    for name, expected in manifest["source_sha256"].items():
        if (
            hashlib.sha256((folder / "source" / name).read_bytes()).hexdigest()
            != expected
        ):
            raise ValueError(f"Measured-source hash mismatch: {name}")
    histories = [value for record in raw.server_queries for value in record.values()]
    qa = {
        "status": "passed",
        "measurements": len(raw),
        "conditions": len(summary),
        "history_expected": len(histories),
        "history_found": sum(value is not None for value in histories),
        "history_missing": sum(value is None for value in histories),
        "statistics": "Mean and sample SD independently cross-checked using statistics and pandas",
        "fingerprints": "All measured results match common full-result reference fingerprints",
        "visual_review": "pending",
    }
    flattened = raw.copy()
    for column in ("fingerprint", "query_ids", "server_queries"):
        flattened[column] = flattened[column].map(json.dumps)
    flattened.to_csv(folder / "measurements.csv", index=False)
    summary.to_csv(folder / "summary.csv", index=False)
    summary.to_json(
        folder / "summary.json", orient="records", indent=2, force_ascii=False
    )
    images = [
        draw(folder, summary, size, manifest)
        for size in sorted(manifest["settings"]["sizes"])
    ]
    (folder / "qa.json").write_text(json.dumps(qa, indent=2))
    (folder / "report.md").write_text(report_text(summary, manifest, calls, qa, images))
