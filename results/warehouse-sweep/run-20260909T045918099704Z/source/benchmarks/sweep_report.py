"""Six-path summaries and vertical mean/SD figures with explicit timing boundaries."""

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
from benchmarks.sweep_benchmark import PATHS

LABELS = {
    "pandas": "로컬\npandas",
    "duckdb": "로컬\nDuckDB",
    "snowflake_xsmall": "Snowflake\nX-Small",
    "snowflake_small": "Snowflake\nSmall",
    "snowflake_medium": "Snowflake\nMedium",
    "snowflake_large": "Snowflake\nLarge",
}
WORKS = {
    "clean_derive": "결측치 처리 · 파생 컬럼",
    "filter_project": "필터 · 파생 컬럼",
    "groupby": "지역별 집계",
    "join_groupby": "조인 후 집계",
}
COLORS = ["#3268B2", "#C77C22", "#BBC096", "#9CA566", "#788246", "#515C2C"]
HATCHES = [None, "///", "...", "///", "xx", "++"]
KEYS = ["rows", "workload", "path"]


def audit(raw, manifest, validations, references):
    settings = manifest["settings"]
    groups = raw.groupby(KEYS)
    expected = set(itertools.product(settings["sizes"], WORKLOADS, PATHS))
    if set(groups.groups) != expected or raw.duplicated([*KEYS, "run"]).any():
        raise ValueError("Incomplete condition matrix or duplicate samples")
    checks = {(v["rows"], v["workload"], v["path"], v["block"]): v for v in validations}
    if len(checks) != len(expected) * settings["blocks"] or len(checks) != len(
        validations
    ):
        raise ValueError("Missing or duplicate block validation")
    records = []
    for key, samples in groups:
        if sorted(samples.run) != list(range(1, settings["runs"] + 1)):
            raise ValueError(f"Wrong run count: {key}")
        reference = references[f"{key[0]}/{key[1]}"]
        for sample in samples.to_dict("records"):
            block = (sample["run"] - 1) // (settings["runs"] // settings["blocks"]) + 1
            if (
                sample["block"] != block
                or checks[(*key, block)]["checksum"] != reference
                or sample["output_rows"] != reference["rows"]
            ):
                raise ValueError(f"Output or validation mismatch: {key}")
            if not math.isfinite(sample["elapsed_ms"]) or sample["elapsed_ms"] <= 0:
                raise ValueError("Invalid elapsed time")
            if key[2].startswith("snowflake"):
                if (
                    sample["metric_scope"] != "platform_query"
                    or sample["elapsed_ms"] != sample["server_total_ms"]
                    or not sample["query_id"]
                ):
                    raise ValueError("Wrong Snowflake metric boundary")
            elif sample["metric_scope"] != "local_parquet_to_batches":
                raise ValueError("Wrong local metric boundary")
        values = samples.elapsed_ms.tolist()
        records.append(
            dict(
                zip(KEYS, key, strict=True),
                n=len(values),
                mean_ms=statistics.mean(values),
                std_ms=statistics.stdev(values) if len(values) > 1 else None,
                median_ms=statistics.median(values),
                min_ms=min(values),
                max_ms=max(values),
            )
        )
    remote = raw.loc[raw.path.str.startswith("snowflake")]
    if remote.query_id.duplicated().any():
        raise ValueError("Duplicate Snowflake query IDs")
    return pd.DataFrame(records)


def draw(folder, summary, rows, manifest):
    font = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = "Apple SD Gothic Neo"
    plt.rcParams["axes.unicode_minus"] = False
    data = summary.loc[summary.rows.eq(rows)].set_index(["workload", "path"])
    fig, axes = plt.subplots(2, 2, figsize=(19, 12.5))
    fig.subplots_adjust(
        left=0.065, right=0.985, top=0.80, bottom=0.19, wspace=0.22, hspace=0.58
    )
    for ax, (work, title) in zip(axes.flat, WORKS.items(), strict=True):
        samples = [data.loc[(work, path)] for path in PATHS]
        maximum = max(
            s.mean_ms + (0 if pd.isna(s.std_ms) else s.std_ms) for s in samples
        )
        for x, (sample, color, hatch) in enumerate(
            zip(samples, COLORS, HATCHES, strict=True)
        ):
            mean, sd = sample.mean_ms, sample.std_ms
            ax.bar(
                x,
                mean,
                width=0.60,
                color=color,
                edgecolor=color,
                hatch=hatch,
                alpha=0.88,
            )
            if pd.notna(sd):
                ax.errorbar(
                    x,
                    mean,
                    yerr=[[min(mean, sd)], [sd]],
                    fmt="none",
                    color="#25282D",
                    capsize=4,
                )
            if pd.notna(sd) and sd > mean:
                ax.plot(x, 0, marker="v", color="#25282D", clip_on=False)
            label = (
                f"{mean:,.2f}\n± {sd:,.2f}" if pd.notna(sd) else f"{mean:,.2f}\nSD 미정"
            )
            ax.text(
                x,
                mean + (0 if pd.isna(sd) else sd) + maximum * 0.045,
                label,
                ha="center",
                va="bottom",
                fontsize=12,
            )
        ax.set_xticks(range(6), [LABELS[p] for p in PATHS], fontsize=12)
        ax.set_ylim(0, maximum * 1.38)
        ax.set_xlim(-0.6, 5.6)
        ax.set_ylabel("실행 시간 (ms)", fontsize=13)
        ax.set_title(title, loc="left", fontsize=22, pad=16)
        ax.yaxis.set_major_locator(MaxNLocator(4, min_n_ticks=3))
        ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
        ax.tick_params(length=0, pad=7, labelsize=12)
        ax.grid(axis="y", color="#DFE2E6")
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
    phase = "파일럿" if manifest["settings"]["pilot"] else "본 실험"
    fig.text(
        0.045,
        0.95,
        "로컬 pandas · DuckDB와 Snowflake warehouse 크기별 성능",
        fontsize=28,
    )
    fig.text(
        0.045,
        0.91,
        f"{rows:,} 행  |  {phase} · 조건별 {manifest['settings']['runs']}회  |  평균 ± 표본 SD (ms)  |  {manifest['started_at'][:10]}",
        fontsize=18,
    )
    fig.text(
        0.045,
        0.872,
        "로컬: Parquet → 모든 결과 배치 생성   ·   Snowflake: 적재 테이블 → 서버의 전체 쿼리 시간",
        fontsize=17,
    )
    notes = [
        "Snowflake 전체 결과의 Mac 다운로드·최초 업로드/적재 제외  ·  SQL 실행·컴파일·대기 시간은 별도 기록",
        "로컬은 결과 배치를 순차 생성하고 해제  ·  모든 크기에 같은 처리 기준  ·  같은 하드웨어의 엔진 비교가 아님",
        "작업별 축 범위가 다름  ·  0 기준 선형 축  ·  오차막대 = 평균 ± 표본 SD, 신뢰구간 아님  ·  ▼: 음수 SD 하한을 0에서 자름",
    ]
    for y, note in zip((0.115, 0.077, 0.039), notes, strict=True):
        fig.text(0.045, y, note, fontsize=14, color="#50555D")
    name = f"mean-sd-{rows}.png"
    fig.savefig(folder / name, dpi=160, facecolor="white")
    plt.close(fig)
    return name


def render(folder, records, manifest, validations, references):
    raw = pd.DataFrame(records)
    summary = audit(raw, manifest, validations, references)
    for name, digest in manifest["source_sha256"].items():
        if (
            hashlib.sha256((folder / "source" / name).read_bytes()).hexdigest()
            != digest
        ):
            raise ValueError(f"Source snapshot hash mismatch: {name}")
    summary.to_csv(folder / "summary.csv", index=False)
    summary.to_json(
        folder / "summary.json", orient="records", indent=2, force_ascii=False
    )
    raw.to_csv(folder / "measurements.csv", index=False)
    components = []
    for key, g in raw.loc[raw.path.str.startswith("snowflake")].groupby(KEYS):
        for column in [
            "server_total_ms",
            "server_execution_ms",
            "server_compilation_ms",
            "server_provisioning_queue_ms",
            "server_overload_queue_ms",
            "client_execute_roundtrip_ms",
        ]:
            values = g[column].dropna().tolist()
            components.append(
                dict(
                    zip(KEYS, key, strict=True),
                    component=column,
                    n=len(values),
                    mean_ms=statistics.mean(values) if values else None,
                    std_ms=statistics.stdev(values) if len(values) > 1 else None,
                )
            )
    pd.DataFrame(components).to_csv(folder / "snowflake-components.csv", index=False)
    images = [
        draw(folder, summary, size, manifest) for size in manifest["settings"]["sizes"]
    ]
    qa = {
        "status": "passed",
        "measurements": len(raw),
        "conditions": len(summary),
        "block_full_checksums": len(validations),
        "every_run": "Output row count checked; full checksums verified per block, not per measured run",
        "visual_review": "pending",
        "metric_boundaries": "Local Parquet to all output batches; Snowflake platform total query time. Full remote result download excluded.",
    }
    (folder / "qa.json").write_text(json.dumps(qa, indent=2))
    lines = [
        "# 로컬 pandas·DuckDB와 Snowflake 4개 warehouse 크기 비교",
        "",
        f"**{len(summary)}개 조건 × {manifest['settings']['runs']}회 = {len(raw):,}회**를 완료했다. 데이터 크기마다 로컬 pandas·DuckDB 및 Snowflake X-Small·Small·Medium·Large의 6개 실행 경로를 비교한다.",
        "",
        "주 지표는 각 환경에서 입력을 읽고 결과 생성을 완료하는 시간이며 낮을수록 빠르다. 로컬은 Parquet를 읽어 전체 결과를 pandas 배치로 순차 생성한다. Snowflake는 적재 테이블의 SQL이 끝났을 때 query history의 total_elapsed_time을 사용하며 서버 실행·컴파일·대기를 포함한다. Snowflake의 전체 결과를 Mac으로 다운로드하지 않는다. 로컬 프로세스 시작·라이브러리 로딩·DuckDB 연결 생성과 종료, Snowflake 연결은 제외한다. 같은 CPU·RAM이나 같은 입력 저장 형식의 순수 엔진 비교는 아니다.",
        "",
        "모든 크기에 배치 처리 기준을 적용해 큰 결과 전체를 메모리에 누적하지 않는다. pandas는 Parquet 배치별 변환 후 집계의 부분 합을 합치며, DuckDB는 Parquet 직접 SQL 결과를 Arrow reader로 읽는다. 결과 행 수와 모든 컬럼을 사용하는 두 모듈러 체크섬은 각 묶음 예열에서 확인하고, 시간 측정 반복은 결과 행 수를 확인한다. 체크섬·예열·최초 적재는 주 측정 밖이다. 체크섬은 비암호화 검증이며 완전한 원격 결과 본문은 보관하지 않는다.",
        "",
        "## 행 수별 평균 ± 표본 표준편차",
        "",
        "SD는 반복 변동성이며 신뢰구간이 아니다. 모든 축은 0에서 시작하며 패널별 범위가 다르다.",
        "",
    ]
    for size, image in zip(manifest["settings"]["sizes"], images, strict=True):
        lines.extend(
            [
                f"### {size:,}행",
                "",
                f"![{size:,}행 평균과 표준편차]({image})",
                "",
                "| 작업 | pandas | DuckDB | SF X-Small | SF Small | SF Medium | SF Large |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for work, title in WORKS.items():
            cells = []
            for path in PATHS:
                row = summary.loc[
                    summary.rows.eq(size)
                    & summary.workload.eq(work)
                    & summary.path.eq(path)
                ].iloc[0]
                cells.append(
                    f"{row.mean_ms:,.2f} ± {row.std_ms:,.2f}"
                    if pd.notna(row.std_ms)
                    else f"{row.mean_ms:,.2f} (SD 미정)"
                )
            lines.append("| " + " | ".join([title, *cells]) + " |")
        lines.append("")
    lines.extend(
        [
            "## 조건과 해석 범위",
            "",
            f"- 실행 시각(UTC): {manifest['started_at']} ~ {manifest['completed_at']}",
            f"- 반복: {manifest['settings']['blocks']}개 묶음, 묶음별 경로 순서 및 경로 내 크기·작업 순서 무작위. 한 번에 한 경로만 측정.",
            f"- 로컬: {manifest['local_environment']['platform']}, 논리 CPU {manifest['local_environment']['cpu_logical']}개, RAM {manifest['local_environment']['ram_bytes'] / 2**30:.1f}GiB. DuckDB/Arrow 스레드 {manifest['settings']['threads']}개, DuckDB 내부 메모리 {manifest['settings']['memory_gb']}GB, 입력/출력 배치 최대 {manifest['settings']['batch_rows']:,}행.",
            "- Snowflake: 네 전용 Standard warehouse, 단일 클러스터·자동 중지 60초·결과 재사용 캐시 해제. 경로 묶음이 끝나면 해당 전용 warehouse를 중지한다. 각 작업의 실제 SQL을 한 번 예열하지만 모든 데이터 캐시가 같은 상태임을 보장하지 않는다.",
            "- pandas는 컬럼 선택·필터 pushdown을 적용한다. DuckDB는 Parquet 직접 SQL을 사용하므로 입력 스캔과 계산 시간을 임의로 분리하지 않는다.",
            "- Snowflake의 client_execute_roundtrip_ms는 호출·응답 관측값이며 전체 결과 다운로드 시간이 아니다. 서버 지표들과 범위가 겹쳐 더해서 전체 시간을 만들지 않는다.",
            "- 합성 수치형 8개 컬럼과 계정 10만 행, seed 20260907의 네 작업이다. 이전 20개 컬럼·계정 100만 행의 4개 DB 실험과 다른 데이터이며, 이번 수치를 그 순위에 합치지 않는다.",
            "- 작은 입력에서는 고정 준비 비용이 비중을 차지할 수 있다. 큰 warehouse가 항상 빨라지는지, 평균 차이가 SD에 비해 충분히 큰지는 실제 크기·작업별 수치로 판단한다. 비용이나 동시성의 종합 순위는 측정하지 않았다.",
            "",
            "```mermaid",
            "flowchart LR",
            '  P["공통 Parquet"] --> A["로컬 pandas: 배치 변환"]',
            '  P --> D["로컬 DuckDB: Parquet SQL"]',
            '  P --> L["측정 전 업로드·COPY"]',
            '  L --> T["공통 Snowflake 테이블"]',
            '  T --> W["X-Small / Small / Medium / Large SQL"]',
            '  A --> B["전체 결과 배치 생성 시간"]',
            "  D --> B",
            '  W --> Q["서버 전체 쿼리 시간"]',
            '  B --> R["행 수별 6개 평균·SD 비교"]',
            "  Q --> R",
            "```",
            "",
            "## 원자료와 검증",
            "",
            "[원시 측정](measurements.jsonl) · [평균/SD CSV](summary.csv) · [Snowflake 시간 구성요소](snowflake-components.csv) · [실행 명세](manifest.json) · [묶음별 전체 결과 검증](block-validations.json) · [공통 기준값](validation-reference.json) · [QA](qa.json)",
            "",
            "재현: `uv run --no-sync python main.py warehouse-sweep` (`--pilot`은 기능 검증). 키체인 인증을 재사용한다. 원자료와 측정 소스 사본은 이 실행 폴더에 보존한다.",
            "",
            "공식 문서: [Snowflake warehouse 개요](https://docs.snowflake.com/en/user-guide/warehouses-overview), [DuckDB Arrow 결과 배치](https://duckdb.org/docs/current/guides/python/export_arrow).",
            "",
        ]
    )
    (folder / "report.md").write_text("\n".join(lines))
