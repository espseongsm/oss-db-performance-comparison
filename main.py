"""Run the database benchmark one engine at a time."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE_FILE = ROOT / "docker-compose.yml"
PROJECT_NAME = "db-performance-comparison"
ENGINES = ("clickhouse", "duckdb", "sqlite", "postgres")
AVAILABLE_ENGINES = (*ENGINES, "doris")
ENGINE_VOLUMES = {
    "clickhouse": (f"{PROJECT_NAME}_clickhouse_data", f"{PROJECT_NAME}_clickhouse_logs"),
    "duckdb": (f"{PROJECT_NAME}_duckdb_data",),
    "sqlite": (f"{PROJECT_NAME}_sqlite_data",),
    "postgres": (f"{PROJECT_NAME}_postgres_data",),
    "doris": tuple(
        f"{PROJECT_NAME}_{name}"
        for name in ("doris_data", "doris_meta", "doris_be_logs", "doris_fe_logs")
    ),
}
STORAGE_PATHS = {
    "clickhouse": ("clickhouse", "/var/lib/clickhouse", "/var/log/clickhouse-server"),
    "duckdb": ("duckdb", "/data/duckdb"),
    "sqlite": ("sqlite", "/data/sqlite"),
    "postgres": ("postgres", "/var/lib/postgresql/data"),
    "doris": (
        "doris",
        "/opt/apache-doris/be/storage",
        "/opt/apache-doris/fe/doris-meta",
        "/opt/apache-doris/be/log",
        "/opt/apache-doris/fe/log",
    ),
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
    result_root: Path | None = None,
) -> None:
    result_root = result_root or ROOT / "results"
    results_path = Path("/results") / result_root.relative_to(ROOT / "results")
    if profile != "baseline":
        results_path /= profile
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
    if engine in ("postgres", "clickhouse", "doris"):
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


def record_storage_size(
    engine: str, rows: int, profile: str, result_root: Path | None = None
) -> None:
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
    result_root = result_root or ROOT / "results"
    if profile != "baseline":
        result_root /= profile
    result_path = result_root / engine / "storage.json"
    result_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(
        f"{engine}: 저장 크기 {total_bytes / 2**30:.2f} GiB 기록 완료",
        flush=True,
    )


def record_deployment(
    result_root: Path, services: tuple[str, ...], *, capture_sources: bool = False
) -> None:
    def docker_output(*arguments: str) -> str:
        return subprocess.run(
            ["docker", *arguments], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout

    container_ids = docker_output(
        "compose", "-p", PROJECT_NAME, "-f", str(COMPOSE_FILE), "ps", "-q", *services
    ).splitlines()
    containers = json.loads(docker_output("inspect", *container_ids))
    images = json.loads(
        docker_output(
            "image",
            "inspect",
            *sorted({container["Image"] for container in containers}),
        )
    )
    daemon = json.loads(docker_output("info", "--format", "{{json .}}"))
    snapshot = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
        },
        "docker": {
            key: daemon.get(key)
            for key in (
                "ServerVersion",
                "OperatingSystem",
                "OSType",
                "Architecture",
                "KernelVersion",
                "NCPU",
                "MemTotal",
            )
        },
        "containers": [
            {
                "id": container["Id"],
                "service": container["Config"]["Labels"]["com.docker.compose.service"],
                "requested_image": container["Config"]["Image"],
                "image_id": container["Image"],
                "limits": {
                    key: container["HostConfig"].get(key)
                    for key in (
                        "NanoCpus",
                        "CpuQuota",
                        "CpuPeriod",
                        "CpusetCpus",
                        "Memory",
                        "MemorySwap",
                        "ShmSize",
                    )
                },
            }
            for container in containers
        ],
        "images": [
            {
                key: image.get(key)
                for key in ("Id", "RepoTags", "RepoDigests", "Architecture", "Os")
            }
            for image in images
        ],
        "note": "Actual container resource limits and Docker VM totals at recorded_at; zero Memory or NanoCpus means no container override.",
    }
    result_root.mkdir(parents=True, exist_ok=True)
    if capture_sources:
        snapshot["source_sha256"] = {}
        for relative in (
            "main.py", "scripts/engine_worker.py", "scripts/doris_sql.py",
            "scripts/doris_client.py", "docker-compose.yml",
            "docker/runner/Dockerfile", "requirements-runner.txt",
        ):
            saved = result_root / "source" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            if not saved.exists():
                saved.write_bytes((ROOT / relative).read_bytes())
            snapshot["source_sha256"][relative] = hashlib.sha256(
                saved.read_bytes()
            ).hexdigest()
    (result_root / "deployment.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2)
    )


def run_engine(
    engine: str,
    rows: int,
    runs: int,
    force_reload: bool,
    purge_data: bool,
    profile: str,
    result_root: Path | None = None,
) -> None:
    if purge_data:
        purge_engine_volumes(engine)
    services = services_for(engine)
    print(f"\n=== {engine}: 컨테이너 시작 ===", flush=True)
    compose("up", "-d", "--build", *services)
    try:
        if engine == "doris":
            record_deployment(
                result_root or ROOT / "results", services, capture_sources=True
            )
        worker(
            services[0],
            engine,
            "setup",
            rows,
            runs,
            profile,
            force_reload=force_reload or purge_data,
            result_root=result_root,
        )
        worker(
            services[0],
            engine,
            "validate",
            rows,
            runs,
            profile,
            result_root=result_root,
        )
        worker(
            services[0], engine, "measure", rows, runs, profile, result_root=result_root
        )
        record_storage_size(engine, rows, profile, result_root)
        print(f"=== {engine}: 완료 ===", flush=True)
    finally:
        compose("down", "--remove-orphans", check=False)
        if purge_data:
            purge_engine_volumes(engine)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=AVAILABLE_ENGINES, action="append")
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
        "--output-dir", type=Path, help="새 실행 결과를 저장할 results 하위 디렉터리"
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="파이프라인 검증용 10,000행/1회 실행으로 --rows와 --runs를 대체합니다.",
    )
    return parser.parse_args()


def select_result_root(args: argparse.Namespace, engines: tuple[str, ...]) -> Path:
    results = ROOT / "results"
    if args.output_dir is None:
        if not args.pilot and "doris" not in engines:
            return results
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        kind = "pilot" if args.pilot else "run"
        suite = "doris-sql" if "doris" in engines else "sql"
        return results / suite / f"{kind}-{stamp}"
    result_root = args.output_dir.resolve()
    if result_root == results or not result_root.is_relative_to(results):
        raise SystemExit(
            "--output-dir는 프로젝트 results의 새 하위 디렉터리여야 합니다."
        )
    if result_root.exists():
        raise SystemExit("--output-dir가 이미 존재합니다. 새 디렉터리를 지정하세요.")
    return result_root


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "financebench":
        from benchmarks.financebench import main as financebench_main

        return financebench_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "warehouse-sweep":
        from benchmarks.sweep_benchmark import main as sweep_main

        return sweep_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "warehouse-frames":
        from benchmarks.warehouse_benchmark import main as warehouse_main

        return warehouse_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "frames":
        from benchmarks.frame_benchmark import main as frames_main

        return frames_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "frames-doris":
        from benchmarks.frame_doris_benchmark import main as frames_doris_main

        return frames_doris_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "frames-doris-report":
        from benchmarks.frame_doris_report import main as frames_doris_report_main

        return frames_doris_report_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "doris-compare":
        from scripts.doris_comparison import main as doris_comparison_main

        return doris_comparison_main(sys.argv[2:])
    args = parse_args()
    if args.pilot:
        args.rows = 10_000
        args.runs = 1
    if args.rows <= 0 or args.runs <= 0:
        raise SystemExit("--rows와 --runs는 양수여야 합니다.")

    engines = tuple(args.engine or ENGINES)
    results_dir = select_result_root(args, engines)
    ensure_docker()
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
            results_dir,
        )

    manifest = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "rows": args.rows,
        "runs": args.runs,
        "engines": list(engines),
        "sequential": True,
        "purge_data_after_engine": args.purge_data_after_engine,
    }
    results_dir.mkdir(parents=True, exist_ok=True)
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
