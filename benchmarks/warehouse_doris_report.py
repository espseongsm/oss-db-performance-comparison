"""Expand the curated warehouse report using saved, differently scoped Doris runs."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.frame_workloads import WORKLOADS
from benchmarks.sweep_report import WORKS, audit, draw

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "results/warehouse-sweep/run-20260909T045918099704Z"
DORIS = ROOT / "results/doris/frames/run-20261006T000356880611Z"
REFERENCE = ROOT / "results/pandas-duckdb/run-20260907T022808832856Z"
BACKUP = ROOT / "results/doris/original-report-backup/warehouse-sweep"
DORIS_SCOPE = "client_sql_to_full_pandas"
FIELDS = [
    "rows",
    "workload",
    "path",
    "n",
    "mean_ms",
    "std_ms",
    "median_ms",
    "min_ms",
    "max_ms",
    "metric_scope",
    "measured_date",
]


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text())


def checked_results(folder: Path, doris: Path):
    manifest = load_json(folder / "manifest.json")
    records = [
        json.loads(line)
        for line in (folder / "measurements.jsonl").read_text().splitlines()
    ]
    summary = audit(
        pd.DataFrame(records),
        manifest,
        load_json(folder / "block-validations.json"),
        load_json(folder / "validation-reference.json"),
    )
    saved = pd.read_csv(folder / "summary.csv").set_index(["rows", "workload", "path"])
    current = summary.set_index(["rows", "workload", "path"])
    if set(current.index) != set(saved.index) or not np.allclose(
        current.loc[saved.index, ["mean_ms", "std_ms"]],
        saved[["mean_ms", "std_ms"]],
        rtol=1e-12,
        atol=1e-8,
    ):
        raise ValueError("Historical summaries differ from raw measurements")
    for name, expected in manifest["source_sha256"].items():
        if digest(folder / "source" / name) != expected:
            raise ValueError(f"Historical measured source changed: {name}")
    new_manifest = load_json(doris / "manifest.json")
    if new_manifest["status"] != "complete" or new_manifest["measurement_count"] != 360:
        raise ValueError("A completed 360-observation Doris run is required")
    for name, expected in new_manifest["source_sha256"].items():
        if digest(doris / "source" / name) != expected:
            raise ValueError(f"Doris measured source changed: {name}")
    for name, expected in new_manifest["reference_sha256"].items():
        if digest(REFERENCE / name) != expected:
            raise ValueError(f"Doris historical reference changed: {name}")
    datasets = {r["rows"]: r for r in load_json(folder / "datasets.json")}
    new_datasets = {r["rows"]: r for r in load_json(doris / "datasets.json")}
    if set(new_datasets) != {100_000, 1_000_000, 10_000_000} or any(
        datasets[n] != record for n, record in new_datasets.items()
    ):
        raise ValueError("The shared input metadata and checksums must match")
    workload_source = "benchmarks/frame_workloads.py"
    if (
        manifest["source_sha256"][workload_source]
        != new_manifest["source_sha256"][workload_source]
    ):
        raise ValueError("Saved transformation definitions differ")
    fingerprints = load_json(doris / "validation.json")
    if fingerprints != load_json(REFERENCE / "validation.json"):
        raise ValueError("Doris full-result fingerprints differ from their reference")
    references = load_json(folder / "validation-reference.json")
    expected_keys = {
        f"{n}/{work}" for n, work in itertools.product(new_datasets, WORKLOADS)
    }
    if set(fingerprints) != expected_keys or any(
        fingerprints[key][field] != references[key][field]
        for key in fingerprints
        for field in ("rows", "columns")
    ):
        raise ValueError(
            "Shared output shapes differ; checksum algorithms are not interchangeable"
        )
    raw = pd.read_csv(doris / "measurements.csv")
    json_records = pd.DataFrame(
        [
            json.loads(line)
            for line in (doris / "measurements.jsonl").read_text().splitlines()
        ]
    )
    pd.testing.assert_frame_equal(
        raw, json_records, check_exact=False, rtol=1e-12, atol=1e-8
    )
    if (
        len(raw) != 360
        or not raw.validated.eq(True).all()
        or not raw.engine.eq("doris").all()
        or not raw["mode"].eq("warehouse").all()
        or not np.isfinite(raw.elapsed_ms).all()
        or not raw.elapsed_ms.gt(0).all()
    ):
        raise ValueError("Invalid Doris observations")
    grouped = raw.groupby(["rows", "workload"])
    if set(grouped.groups) != set(itertools.product(new_datasets, WORKLOADS)):
        raise ValueError("Incomplete Doris condition matrix")
    for (n, work), samples in grouped:
        if (
            sorted(samples.run) != list(range(1, 31))
            or not samples.block.eq((samples.run - 1) // 5 + 1).all()
            or not samples.output_rows.eq(references[f"{n}/{work}"]["rows"]).all()
        ):
            raise ValueError(f"Invalid Doris repetitions or output counts: {n}/{work}")
    new_summary = grouped.elapsed_ms.agg(
        n="size",
        mean_ms="mean",
        std_ms="std",
        median_ms="median",
        min_ms="min",
        max_ms="max",
    ).reset_index()
    new_saved = pd.read_csv(doris / "summary.csv").set_index(["rows", "workload"])
    if not np.allclose(
        new_summary.set_index(["rows", "workload"]).loc[
            new_saved.index, ["mean_ms", "std_ms"]
        ],
        new_saved[["mean_ms", "std_ms"]],
        rtol=1e-12,
        atol=1e-8,
    ):
        raise ValueError("Doris summary differs from raw observations")
    new_summary["path"] = "doris"
    new_summary["metric_scope"] = DORIS_SCOPE
    new_summary["measured_date"] = new_manifest["started_at"][:10]
    summary["metric_scope"] = np.where(
        summary.path.str.startswith("snowflake"),
        "platform_query",
        "local_parquet_to_batches",
    )
    summary["measured_date"] = manifest["started_at"][:10]
    return summary[FIELDS], new_summary[FIELDS], manifest, new_manifest


def extend_text(original: str, summary: pd.DataFrame, new: pd.DataFrame) -> str:
    body = original.replace(
        "# 로컬 pandas·DuckDB와 Snowflake 4개 warehouse 크기 비교",
        "# 로컬 pandas·DuckDB와 Snowflake 4개 warehouse 크기 비교 — Doris 기존 결과 추가",
        1,
    )
    intro = (
        "**Doris 기존 결과 추가(2026-10-07 KST):** 2026-10-06에 완료한 Doris 12개 조건·360회를 재사용해 "
        "총 **132개 조건·3,960개 저장 관측**을 함께 표시했다. 새 벤치마크는 실행하지 않았다. "
        "Doris는 10만·100만·1,000만 행만 측정했고 **1억·10억 행은 미측정**이다. "
        "†열·막대는 SQL 요청부터 전체 결과 수신·DataFrame 생성·int64 정규화까지의 별도 경로이며, "
        "Snowflake 서버 시간과 동등한 서버 처리 시간으로 비교하지 않는다.\n\n"
    )
    body = body.replace("**10억 행에서는", intro + "**10억 행에서는", 1)
    body = body.replace(
        "10만~1,000만 행에서는 단순 가공",
        "기존 6개 경로의 10만~1,000만 행에서는 단순 가공",
        1,
    )
    body = body.replace(
        "모든 크기에 배치 처리 기준을 적용해",
        "기존 로컬 두 경로는 모든 크기에 배치 처리 기준을 적용해",
        1,
    )
    body = body.replace(
        "사후 통계·입력·차트 검수 및 본문 갱신일은",
        "원보고서의 사후 통계·입력·차트 검수 및 본문 갱신일은",
        1,
    )
    body = body.replace(
        "아래 표는 이번에 측정한 크기에서의 실행시간을 기준으로 한 선택 가이드다.",
        "아래 표는 기존 6개 경로의 실행시간을 기준으로 한 선택 가이드다. "
        "측정 범위가 다른 Doris 참조 열은 이 순위에 합치지 않는다.",
        1,
    )
    body = body.replace(
        "작은 입력에서는 로컬이 모든 작업에서 앞섰다.",
        "기존 6개 경로의 작은 입력에서는 로컬이 모든 작업에서 앞섰다.",
        1,
    )
    body = body.replace(
        "단순 가공은 pandas 11.19ms", "기존 6개 경로에서 단순 가공은 pandas 11.19ms", 1
    )
    body = body.replace(
        "여전히 단순 가공은 pandas, 집계는 DuckDB가 가장 빨랐다.",
        "기존 6개 경로에서는 단순 가공은 pandas, 집계는 DuckDB가 가장 빨랐다.",
        1,
    )
    old = summary.set_index(["rows", "workload", "path"])
    doris = new.set_index(["rows", "workload"])
    grouped = doris.loc[(10_000_000, "groupby")]
    joined = doris.loc[(10_000_000, "join_groupby")]
    clean = doris.loc[(10_000_000, "clean_derive")]
    filtered = doris.loc[(10_000_000, "filter_project")]
    duck_group = old.loc[(10_000_000, "groupby", "duckdb")]
    duck_join = old.loc[(10_000_000, "join_groupby", "duckdb")]
    doris_text = (
        "### Doris의 기존 결과를 함께 볼 때\n\n"
        f"**이미 서버에 데이터가 있고 작은 집계 결과를 Python으로 받는 경로에서는 Doris도 검토 후보다.** "
        f"1,000만 행의 지역 집계는 {grouped.mean_ms:.3f} ± {grouped.std_ms:.3f}ms, "
        f"조인 후 집계는 {joined.mean_ms:.3f} ± {joined.std_ms:.3f}ms였다. "
        f"기존 DuckDB 배치 경로는 각각 {duck_group.mean_ms:.3f} ± {duck_group.std_ms:.3f}ms와 "
        f"{duck_join.mean_ms:.3f} ± {duck_join.std_ms:.3f}ms였다. 지역 집계의 관측 평균은 Doris가 낮았고 "
        "조인 집계는 가까웠지만, 실행일·배포·입출력 범위가 달라 동일 조건의 엔진 우위나 유의성을 확정하지 않는다.\n\n"
        f"**많은 행을 Python으로 반환하는 경로는 별도로 봐야 한다.** 1,000만 행의 결측치 처리·파생 컬럼은 "
        f"{clean.mean_ms / 1000:.3f}초, 필터·파생 컬럼은 {filtered.mean_ms / 1000:.3f}초였다. "
        "Doris는 완전한 DataFrame을 만들며 기존 로컬 경로는 배치를 순차 생성·해제한다. "
        "이 수치로 대량 결과 다운로드까지 포함한 애플리케이션 경로를 검토할 수 있지만 "
        "서버 처리·통신·클라이언트 변환 중 하나를 원인으로 단정하거나 Snowflake에 같은 다운로드 시간을 가정하지 않는다.\n\n"
        "세 크기의 입력 파일 SHA256과 변환 정의는 정확히 일치한다. Doris 전체 출력 fingerprint는 기존 전처리 "
        "reference와 일치하고 이 warehouse 실험의 출력 컬럼·행 수도 일치한다. 두 실험은 서로 다른 해시 알고리즘을 "
        "사용하므로 hash_sum/hash_xor와 sum_h/sum_h2의 숫자를 직접 대조하지 않았다. "
        "Doris의 execute_roundtrip_ms는 버퍼링된 결과 수신을 포함하므로 서버 실행 시간으로 바꾸어 쓰지 않는다. "
        "1억·10억 행 Doris 성능·warehouse 크기별 상대 배율·비용 효율은 이 자료에서 판단할 수 없다.\n\n"
    )
    body = body.replace(
        "## 행 수별 평균 ± 표본 표준편차",
        doris_text + "## 행 수별 평균 ± 표본 표준편차",
        1,
    )
    labels = {label: work for work, label in WORKS.items()}
    lines, size, count = [], None, 0
    table_open = False
    note = (
        "† Doris 전체 반환: 사전 적재 테이블의 SQL 요청 → 전체 결과 수신·DataFrame 생성·int64 정규화. "
        "최초 적재·연결·예열·검증 제외. 기존 로컬은 배치 반환, Snowflake는 전체 다운로드를 제외한 서버 시간이다. "
        "서로 다른 측정 범위·실행일의 참조값이며 미측정은 0이 아니다."
    )
    for line in body.splitlines():
        if match := re.fullmatch(r"### ([\d,]+)행", line):
            size = int(match[1].replace(",", ""))
        if line.startswith("| 작업 | pandas |"):
            line = line.rstrip() + " Doris 전체 반환† |"
            table_open = True
        elif table_open and line.startswith("|---"):
            line += "---:|"
        elif table_open and line.startswith("| "):
            cells = [c.strip() for c in line.strip("|").split("|")]
            work = labels[cells[0]]
            record = doris.loc[(size, work)] if (size, work) in doris.index else None
            value = (
                "미측정"
                if record is None
                else f"{record.mean_ms:,.2f} ± {record.std_ms:,.2f}"
            )
            line += f" {value} |"
            count += 1
        elif table_open:
            lines.extend(["", note])
            table_open = False
        lines.append(line)
    if count != 20:
        raise ValueError("Expected exactly 20 existing mean/SD comparison rows")
    body = "\n".join(lines) + "\n"
    scope_note = (
        "- Doris 참조 실행(UTC): 2026-10-06T00:03:56.881002+00:00 ~ 2026-10-06T00:18:10.829090+00:00. "
        "macOS-27.0.1-arm64, Python 3.13.5, pandas 2.3.3, Doris doris-4.1.3-rc02-7126cf65d96. "
        "Docker 전체 4 CPU·8GiB, 단일 클라이언트·warm-cache·SQL/query cache 해제. "
        "기존 9월 9일 측정과 OS·배포·캐시·부하가 같았다고 가정하지 않는다.\n"
    )
    body = body.replace(
        "## 조건과 해석 범위\n\n", "## 조건과 해석 범위\n\n" + scope_note, 1
    )
    evidence = (
        "\n### Doris 추가 자료와 재생성\n\n"
        "- [Doris 저장 원시 결과](../../doris/frames/run-20261006T000356880611Z/measurements.csv), "
        "[실행 명세](../../doris/frames/run-20261006T000356880611Z/manifest.json), "
        "[전체 출력 검증](../../doris/frames/run-20261006T000356880611Z/validation.json).\n"
        "- [Doris 12개 조건 통계](doris-summary.csv), [측정 범위를 표시한 132개 조건](doris-comparison.csv), "
        "[원자료 보존·재생성 검증](doris-render-audit.json), [추가 차트 시각 검수](doris-visual-review.json). 기존 summary/measurements/manifest는 수정하지 않았다.\n"
        "- [추가 전 보고서와 차트](../../doris/original-report-backup/warehouse-sweep/report.md)는 별도 보존한다. "
        "같은 원자료로 재생성: `uv run python main.py warehouse-doris-report`. DB 연결·측정 실행은 하지 않는다.\n"
        "- 독립 본문 검증: `uv run python results/warehouse-sweep/run-20260909T045918099704Z/verify_report.py`. "
        "기존 120개 수치와 추천·Snowflake 배율, 추가 Doris 수치·미측정 표기를 확인한다.\n"
    )
    return body + evidence


def render() -> dict:
    summary, new, manifest, new_manifest = checked_results(FOLDER, DORIS)
    excluded = {
        "report.md",
        "verify_report.py",
        "report-audit.json",
        "doris-summary.csv",
        "doris-comparison.csv",
        "doris-render-audit.json",
        "doris-visual-review.json",
        *(f"mean-sd-{n}.png" for n in manifest["settings"]["sizes"]),
    }
    protected = {
        path.relative_to(FOLDER).as_posix(): digest(path)
        for path in FOLDER.rglob("*")
        if path.is_file()
        and path.relative_to(FOLDER).as_posix() not in excluded
        and "__pycache__" not in path.parts
    }
    doris_protected = {
        path.relative_to(DORIS).as_posix(): digest(path)
        for path in DORIS.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    BACKUP.mkdir(parents=True, exist_ok=True)
    for name in [
        "report.md",
        *(f"mean-sd-{n}.png" for n in manifest["settings"]["sizes"]),
    ]:
        if not (BACKUP / name).exists():
            shutil.copyfile(FOLDER / name, BACKUP / name)
    body = extend_text((BACKUP / "report.md").read_text(), summary, new)
    new.to_csv(FOLDER / "doris-summary.csv", index=False)
    pd.concat([summary, new], ignore_index=True).to_csv(
        FOLDER / "doris-comparison.csv", index=False
    )
    for n in manifest["settings"]["sizes"]:
        draw(FOLDER, summary, n, manifest, doris=new)
    (FOLDER / "report.md").write_text(body)
    if any(
        digest(FOLDER / name) != expected for name, expected in protected.items()
    ) or any(
        digest(DORIS / name) != expected for name, expected in doris_protected.items()
    ):
        raise ValueError("Original evidence changed during report generation")
    result = {
        "status": "passed",
        "updated_on_kst": "2026-10-07",
        "historical_conditions": len(summary),
        "historical_measurements": 3600,
        "doris_conditions": len(new),
        "reused_doris_measurements": 360,
        "new_measurements": 0,
        "shared_input_metadata_and_hashes": "exact match",
        "workload_source_hash": new_manifest["source_sha256"][
            "benchmarks/frame_workloads.py"
        ],
        "validation": "Doris full fingerprints match Sept 7 reference; Sept 9 shapes match. Different checksum algorithms are not equated.",
        "missing_doris_sizes": [100_000_000, 1_000_000_000],
        "metric_scopes": {
            "local": "local_parquet_to_batches",
            "snowflake": "platform_query",
            "doris": DORIS_SCOPE,
        },
        "historical_protected_sha256": protected,
        "doris_protected_sha256": doris_protected,
        "original_report_sha256": digest(BACKUP / "report.md"),
        "report_sha256": digest(FOLDER / "report.md"),
        "charts_sha256": {
            f"mean-sd-{n}.png": digest(FOLDER / f"mean-sd-{n}.png")
            for n in manifest["settings"]["sizes"]
        },
        "renderer_sha256": {
            name: digest(ROOT / name)
            for name in (
                "benchmarks/warehouse_doris_report.py",
                "benchmarks/sweep_report.py",
            )
        },
        "visual_review": "Recorded separately in doris-visual-review.json; generation alone is not visual acceptance.",
    }
    (FOLDER / "doris-render-audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    return {k: v for k, v in result.items() if not k.endswith("sha256")}


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    print(json.dumps(render(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
