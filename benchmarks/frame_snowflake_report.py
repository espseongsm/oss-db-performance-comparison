"""Compare application paths without pretending remote and local inputs match."""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from benchmarks.frame_workloads import LABELS, WORKLOADS


def render_snowflake_comparison(
    folder: Path, raw: pd.DataFrame, summary: pd.DataFrame, manifest: dict
) -> None:
    selected = summary.loc[summary["mode"].isin(["parquet", "warehouse"])].copy()
    selected.to_csv(folder / "three-engine-comparison.csv", index=False)
    remote = raw.loc[raw.engine == "snowflake"].copy()
    fields = [
        column
        for column in (
            "execute_roundtrip_ms",
            "fetch_dataframe_ms",
            "server_total_ms",
            "server_execution_ms",
            "server_compilation_ms",
            "server_provisioning_queue_ms",
            "server_overload_queue_ms",
            "server_bytes_scanned",
        )
        if column in remote.columns
    ]
    remote[fields] = remote[fields].apply(pd.to_numeric)
    remote.groupby(["rows", "workload"])[fields].agg(
        ["count", "mean", "std", "median", "min", "max"]
    ).to_csv(folder / "snowflake-components.csv")
    sizes = sorted(selected.rows.unique())
    colors = {"pandas": "#3268B2", "duckdb": "#C77C22", "snowflake": "#73763B"}
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
    for ax, workload in zip(axes.flat, WORKLOADS, strict=True):
        for offset, engine, marker in [
            (-0.2, "pandas", "o"),
            (0, "duckdb", "s"),
            (0.2, "snowflake", "^"),
        ]:
            part = selected.loc[
                (selected.workload == workload) & (selected.engine == engine)
            ].sort_values("rows")
            x = np.arange(len(sizes)) + offset
            std = part.std_ms.to_numpy()
            mean = part.mean_ms.to_numpy()
            clipped = mean - std <= 0
            lower_error = np.where(clipped, mean * 0.95, std)
            ax.errorbar(
                x,
                mean,
                yerr=[lower_error, std],
                fmt=marker,
                color=colors[engine],
                capsize=3,
                label=engine,
            )
            if clipped.any():
                ax.scatter(
                    x[clipped],
                    mean[clipped] * 0.05,
                    marker="v",
                    color=colors[engine],
                    s=30,
                )
        ax.set(
            title=LABELS[workload],
            ylabel="Mean elapsed time (ms, log scale)",
            yscale="log",
            xticks=np.arange(len(sizes)),
            xticklabels=[f"{rows:,}" for rows in sizes],
            xlabel="Input rows",
        )
        ax.grid(axis="y", alpha=0.18)
        ax.legend(frameon=False)
    fig.suptitle(
        f"Time to a complete pandas result | {manifest['settings']['runs']} runs per case\n"
        "Local: Parquet + transform | Snowflake: preloaded table + SQL + download\n"
        "Error bars: sample SD (v: lower bound <= 0, clipped) | upload excluded; server execution included",
        fontsize=12,
    )
    fig.savefig(folder / "timing-three-engines.png", dpi=160)
    plt.close(fig)
    lines = [
        "",
        "## Snowflake 추가 비교",
        "",
        "**입력 위치와 자원이 다른 애플리케이션 경로 비교다.** 로컬 parquet 결과와 원격 warehouse 결과를 병기한다. "
        "Snowflake 업로드·COPY 비용은 별도이며, 로컬 memory와 동일한 시작 조건이라고 해석하지 않는다.",
        "",
        "| 입력 행 | 작업 | pandas Parquet(ms) | DuckDB Parquet(ms) | Snowflake 적재 테이블(ms) |",
        "|---:|---|---:|---:|---:|",
    ]
    for (rows, workload), group in selected.groupby(["rows", "workload"]):
        by_engine = group.set_index("engine")
        values = [
            f"{by_engine.loc[engine, 'mean_ms']:.3f} ± {by_engine.loc[engine, 'std_ms']:.3f}"
            for engine in ("pandas", "duckdb", "snowflake")
        ]
        lines.append(f"| {rows:,} | {LABELS[workload]} | " + " | ".join(values) + " |")
    lines += [
        "",
        "![3개 엔진의 전체 대기 시간](timing-three-engines.png)",
        "",
        f"- Snowflake 환경: {manifest['snowflake']}",
        "- elapsed_ms는 쿼리 요청부터 fetch_pandas_all 및 컬럼명·int64 정규화 완료까지다. 연결·업로드·COPY·워밍업·검증·query history 조회는 제외한다.",
        "- execute_roundtrip_ms는 서버 시간과 네트워크 왕복을 포함한다. fetch_dataframe_ms는 결과 다운로드·DataFrame 생성을 포함하며 순수 네트워크 시간은 아니다.",
        "- server_* 값은 QUERY_HISTORY에서 query_id로 조회한다. 조회 실패/지연은 빈 값으로 보존하며 count가 실제 관측 수다. 서버 시간은 전체 대기 시간과 별도이며 단순 차를 네트워크 시간으로 해석하지 않는다.",
        "- USE_CACHED_RESULT=FALSE로 결과 재사용을 비활성화한다. warehouse 데이터 캐시·자동 중지는 변경하지 않는다. SQL 워밍업 후 측정하며 서버 재시작·큐 시간도 기록한다.",
        "- 임시 테이블·stage는 실험 세션에 속한다. 지정한 기존 warehouse를 사용하며 생성·크기 변경·강제 중지는 하지 않는다. 세션 종료와 warehouse 중지는 별개이므로 실행 전 계정의 auto_suspend 및 과금 설정을 확인한다.",
        "- Snowflake의 메모리/CPU 서버 자원은 로컬 psutil로 측정하지 않는다. Snowflake RSS는 빈 값, cpu_ms는 로컬 클라이언트 CPU만 의미한다.",
        "- three-engine-comparison.csv: 입력 경로별 기술통계. snowflake-components.csv: 시간 구성요소와 서버 메타데이터 통계. snowflake-setup.json: 업로드·적재 시간. 실제 청구 비용을 추정한 값은 아니다.",
        f"- Query history 조회 오류: {manifest.get('snowflake_history_errors', [])}",
        "- [Python → pandas API](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-pandas)",
        "- [결과 캐시](https://docs.snowflake.com/en/user-guide/querying-persisted-results)",
        "- [서버 쿼리 기록](https://docs.snowflake.com/en/sql-reference/functions/query_history)",
    ]
    with (folder / "report.md").open("a") as handle:
        handle.write("\n".join(lines) + "\n")
