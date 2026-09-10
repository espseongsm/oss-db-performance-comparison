"""Run the database benchmark one engine at a time."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE_FILE = ROOT / "docker-compose.yml"
PROJECT_NAME = "db-performance-comparison"
ENGINES = ("clickhouse", "duckdb", "sqlite", "postgres")
ENGINE_VOLUMES = {
    "clickhouse": (f"{PROJECT_NAME}_clickhouse_data", f"{PROJECT_NAME}_clickhouse_logs"),
    "duckdb": (f"{PROJECT_NAME}_duckdb_data",),
    "sqlite": (f"{PROJECT_NAME}_sqlite_data",),
    "postgres": (f"{PROJECT_NAME}_postgres_data",),
}
STORAGE_PATHS = {
    "clickhouse": ("clickhouse", "/var/lib/clickhouse", "/var/log/clickhouse-server"),
    "duckdb": ("duckdb", "/data/duckdb"),
    "sqlite": ("sqlite", "/data/sqlite"),
    "postgres": ("postgres", "/var/lib/postgresql/data"),
}


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "compose",
        "-p",
        PROJECT_NAME,
        "-f",
        str(COMPOSE_FILE),
        *args,
    ]
    return subprocess.run(command, cwd=ROOT, check=check, text=True)


def ensure_docker() -> None:
    try:
        subprocess.run(
            ["docker", "info"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("Docker CLI를 찾을 수 없습니다.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise SystemExit(f"Docker 데몬에 연결할 수 없습니다: {detail}") from exc


def check_capacity(rows: int, volume_count: int, ignore: bool) -> None:
    if rows < 100_000_000 or ignore:
        return
    free_bytes = shutil.disk_usage(ROOT).free
    estimated_bytes = rows * 20 * 8 * 2.5 * volume_count
    if free_bytes < estimated_bytes:
        free_gib = free_bytes / 2**30
        estimate_gib = estimated_bytes / 2**30
        raise SystemExit(
            f"디스크 용량 부족 가능성이 높습니다: 여유 {free_gib:.1f} GiB, "
            f"보수적 예상 필요량 {estimate_gib:.1f} GiB. "
            "--ignore-capacity-check로 명시적으로 우회할 수 있습니다."
        )


def worker(
    service: str,
    engine: str,
    action: str,
    rows: int,
    runs: int,
    profile: str,
    force_reload: bool = False,
) -> None:
    results_path = "/results" if profile == "baseline" else f"/results/{profile}"
    compose(
        "exec",
        "-T",
        service,
        "env",
        f"BENCH_RESULTS={results_path}",
        f"BENCH_PROFILE={profile}",
        "python",
        "/bench/scripts/engine_worker.py",
        "--engine",
        engine,
        "--action",
        action,
        "--rows",
        str(rows),
        "--runs",
        str(runs),
        "--profile",
        profile,
        *("--force-reload",) if force_reload else (),
    )


def services_for(engine: str) -> tuple[str, ...]:
    if engine in ("postgres", "clickhouse"):
        return ("runner", engine)
    return (engine,)


def purge_engine_volumes(engine: str) -> None:
    volumes = ENGINE_VOLUMES[engine]
    subprocess.run(
        ["docker", "volume", "rm", "-f", *volumes],
        cwd=ROOT,
        check=False,
        text=True,
    )


def record_storage_size(engine: str, rows: int, profile: str) -> None:
    service, *paths = STORAGE_PATHS[engine]
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT_NAME,
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            service,
            "du",
            "-sk",
            *paths,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    path_sizes = {}
    for line in result.stdout.splitlines():
        size_kib, path = line.split(None, 1)
        path_sizes[path] = int(size_kib) * 1024
    total_bytes = sum(path_sizes.values())
    output = {
        "engine": engine,
        "rows": rows,
        "paths": path_sizes,
        "total_bytes": total_bytes,
        "total_gb": total_bytes / 10**9,
        "total_gib": total_bytes / 2**30,
        "note": "Measured immediately before the engine volume is purged.",
    }
    result_root = ROOT / "results"
    if profile != "baseline":
        result_root /= profile
    result_path = result_root / engine / "storage.json"
    result_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(
        f"{engine}: 저장 크기 {total_bytes / 2**30:.2f} GiB 기록 완료",
        flush=True,
    )


def run_engine(
    engine: str,
    rows: int,
    runs: int,
    force_reload: bool,
    purge_data: bool,
    profile: str,
) -> None:
    if purge_data:
        purge_engine_volumes(engine)
    services = services_for(engine)
    print(f"\n=== {engine}: 컨테이너 시작 ===", flush=True)
    compose("up", "-d", "--build", *services)
    try:
        worker(
            services[0],
            engine,
            "setup",
            rows,
            runs,
            profile,
            force_reload=force_reload or purge_data,
        )
        worker(services[0], engine, "validate", rows, runs, profile)
        worker(services[0], engine, "measure", rows, runs, profile)
        record_storage_size(engine, rows, profile)
        print(f"=== {engine}: 완료 ===", flush=True)
    finally:
        compose("down", "--remove-orphans", check=False)
        if purge_data:
            purge_engine_volumes(engine)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=ENGINES, action="append")
    parser.add_argument("--rows", type=int, default=500_000_000)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument(
        "--profile", choices=("baseline", "optimized"), default="baseline"
    )
    parser.add_argument("--force-reload", action="store_true")
    parser.add_argument(
        "--purge-data-after-engine",
        action="store_true",
        help="각 엔진 측정 전후 해당 Docker volume을 삭제합니다.",
    )
    parser.add_argument("--ignore-capacity-check", action="store_true")
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="파이프라인 검증용 10,000행/1회 실행으로 --rows와 --runs를 대체합니다.",
    )
    return parser.parse_args()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "warehouse-frames":
        from benchmarks.warehouse_benchmark import main as warehouse_main

        return warehouse_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "frames":
        from benchmarks.frame_benchmark import main as frames_main

        return frames_main(sys.argv[2:])
    args = parse_args()
    if args.pilot:
        args.rows = 10_000
        args.runs = 1
    if args.rows <= 0 or args.runs <= 0:
        raise SystemExit("--rows와 --runs는 양수여야 합니다.")

    ensure_docker()
    engines = tuple(args.engine or ENGINES)
    volume_count = 1 if args.purge_data_after_engine else len(engines)
    check_capacity(args.rows, volume_count, args.ignore_capacity_check)
    for engine in engines:
        run_engine(
            engine,
            args.rows,
            args.runs,
            args.force_reload,
            args.purge_data_after_engine,
            args.profile,
        )

    manifest = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "rows": args.rows,
        "runs": args.runs,
        "engines": list(engines),
        "sequential": True,
        "purge_data_after_engine": args.purge_data_after_engine,
    }
    results_dir = ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    manifest_path = (
        results_dir / f"experiment-manifest-{args.profile}-{'-'.join(engines)}.json"
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
