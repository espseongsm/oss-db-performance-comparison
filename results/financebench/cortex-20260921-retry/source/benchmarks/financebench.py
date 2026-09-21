"""Reproducible FinanceBench single-client vector database experiment."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from benchmarks.finance_data import DATA, ROOT, prepare, read_jsonl, write_jsonl

ENGINES = (
    "sqlite",
    "duckdb-exact",
    "duckdb-hnsw",
    "postgres",
    "clickhouse",
    "qdrant",
    "weaviate",
    "milvus",
    "duckdb-hnsw-prefilter",
    "clickhouse-prefilter",
)
CLOUD_ENGINES = ("cortex",)
SERVERS = {
    name: name for name in ("postgres", "clickhouse", "qdrant", "weaviate", "milvus")
}
SERVERS["clickhouse-prefilter"] = "clickhouse"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.vector.yml")]


def wait_server(engine):
    import psycopg
    import requests
    from pymilvus import MilvusClient, MilvusException

    urls = {
        "clickhouse": "http://127.0.0.1:18123/ping",
        "qdrant": "http://127.0.0.1:16333/readyz",
        "weaviate": "http://127.0.0.1:18080/v1/.well-known/ready",
    }
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if engine == "postgres":
                with psycopg.connect(
                    "host=localhost port=15432 user=benchmark password=financebench_local dbname=financebench connect_timeout=2"
                ) as db:
                    db.execute("SELECT 1")
            elif engine == "milvus":
                client = MilvusClient(uri="http://localhost:19530", timeout=2)
                client.get_server_version(timeout=2)
                client.close()
            else:
                requests.get(urls[engine], timeout=2).raise_for_status()
            return
        except (
            OSError,
            requests.RequestException,
            psycopg.OperationalError,
            subprocess.TimeoutExpired,
            MilvusException,
        ):
            pass
        time.sleep(1)
    raise TimeoutError(f"{engine} did not become ready in 120 seconds")


def exact_truth(vectors, query, chunks, doc=None, k=10):
    eligible = np.array(
        [i for i, c in enumerate(chunks) if doc is None or c["doc_name"] == doc]
    )
    if not len(eligible):
        return [], float("inf"), np.array([])
    scores = vectors @ query
    order = np.lexsort((eligible, -scores[eligible]))[:k]
    ids = eligible[order].tolist()
    return ids, float(scores[ids[-1]]), scores


def assess(ids, truth, cutoff, scores, chunks, question, doc=None):
    if len(ids) != len(set(ids)) or any(i < 0 or i >= len(chunks) for i in ids):
        raise ValueError("Invalid or duplicate result IDs")
    if any(doc is not None and chunks[i]["doc_name"] != doc for i in ids):
        raise ValueError("Result violates document filter")
    denominator = len(truth)
    relevant = {(e["doc_name"], e["evidence_page_num"]) for e in question["evidence"]}
    returned = {(chunks[i]["doc_name"], chunks[i]["page"]) for i in ids}
    return {
        "ann_recall": len(set(ids) & set(truth)) / denominator if denominator else 1,
        "tie_aware_ann_recall": min(
            sum(scores[i] >= cutoff - 1e-5 for i in ids), denominator
        )
        / denominator
        if denominator
        else 1,
        "evidence_hit": int(bool(relevant & returned)),
        "evidence_page_recall": len(relevant & returned) / len(relevant),
        "shortfall": int(len(ids) < denominator),
    }


def backend(engine, vectors, chunks, directory):
    if engine == "cortex":
        from benchmarks.finance_cortex import CortexBackend

        return CortexBackend(vectors, chunks, directory)
    from benchmarks.finance_local import (
        ClickHouseBackend,
        DuckDBBackend,
        PostgresBackend,
        SQLiteBackend,
    )
    from benchmarks.finance_servers import MilvusBackend, QdrantBackend, WeaviateBackend

    constructors = {
        "sqlite": SQLiteBackend,
        "postgres": PostgresBackend,
        "clickhouse": ClickHouseBackend,
        "qdrant": QdrantBackend,
        "weaviate": WeaviateBackend,
        "milvus": MilvusBackend,
    }
    if engine.startswith("duckdb"):
        return DuckDBBackend(
            vectors,
            chunks,
            directory,
            ann=engine != "duckdb-exact",
            prefilter=engine.endswith("prefilter"),
        )
    if engine == "clickhouse-prefilter":
        return ClickHouseBackend(vectors, chunks, directory, prefilter=True)
    return constructors[engine](vectors, chunks, directory)


def worker(engine, out, repeats):
    from threadpoolctl import threadpool_limits

    threadpool_limits(4)
    out.mkdir(parents=True, exist_ok=False)
    vectors = np.load(DATA / "vectors.npy")
    queries = np.load(DATA / "queries.npy")
    chunks = read_jsonl(DATA / "chunks.jsonl")
    questions = read_jsonl(DATA / "questions.jsonl")
    assert vectors.shape[0] == len(chunks) and queries.shape[0] == len(questions)
    assert np.isfinite(vectors).all() and np.isfinite(queries).all()
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4)
    assert np.allclose(np.linalg.norm(queries, axis=1), 1, atol=1e-4)
    assert [c["id"] for c in chunks] == list(range(len(chunks)))
    start = time.perf_counter()
    db = backend(engine, vectors, chunks, out)
    build = time.perf_counter() - start
    metadata = {
        "engine": engine,
        "version": db.version,
        "load_and_index_seconds": build,
        "plan": db.plan,
        "filtered_plan": getattr(db, "filtered_plan", None),
        "pid": os.getpid(),
        "platform": platform.platform(),
        "repeats": repeats,
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))
    print(f"{engine}: loaded/indexed in {build:.1f}s", flush=True)
    try:
        for mode in ["global", "document-filter"]:
            truth = []
            for q, v in zip(questions, queries):
                doc = q["doc_name"] if mode == "document-filter" else None
                truth.append(exact_truth(vectors, v, chunks, doc))
            # Warm all evaluation queries once. Timing never includes ground truth or metrics.
            for q, v in zip(questions, queries):
                db.search(v, q["doc_name"] if mode == "document-filter" else None)
            with (out / f"{mode}.jsonl").open("w") as f:
                for repeat in range(repeats):
                    order = list(range(len(questions)))
                    random.Random(20260921 + repeat).shuffle(order)
                    for qi in order:
                        q, v = questions[qi], queries[qi]
                        doc = q["doc_name"] if mode == "document-filter" else None
                        begin = time.perf_counter_ns()
                        ids = db.search(v, doc)
                        ms = (time.perf_counter_ns() - begin) / 1e6
                        row = {
                            "engine": engine,
                            "mode": mode,
                            "repeat": repeat,
                            "question_index": qi,
                            "question_id": q["financebench_id"],
                            "latency_ms": ms,
                            "ids": ids,
                            **assess(ids, *truth[qi], chunks, q, doc),
                        }
                        f.write(json.dumps(row) + "\n")
                    f.flush()
            print(f"{engine}: {mode} complete", flush=True)
    finally:
        db.close()


def run(out, engines, repeats):
    out.mkdir(parents=True, exist_ok=False)
    status = []
    manifest = json.loads((DATA / "manifest.json").read_text())
    manifest.update(
        {
            "platform": platform.platform(),
            "python": sys.version,
            "repeats": repeats,
            "engines": engines,
            "concurrency": 1,
            "top_k": 10,
            "seed": 20260921,
            "packages": {
                d.metadata["Name"]: d.version
                for d in importlib.metadata.distributions()
            },
        }
    )
    import psutil

    manifest["host"] = {
        "logical_cpus": psutil.cpu_count(),
        "memory_bytes": psutil.virtual_memory().total,
        "load_average": os.getloadavg(),
    }
    manifest["source_hashes"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            *sorted((ROOT / "benchmarks").glob("finance*.py")),
            ROOT / "main.py",
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
            ROOT / "docker-compose.vector.yml",
        ]
    }
    for name in manifest["source_hashes"]:
        target = out / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    for engine in engines:
        if engine in SERVERS:
            image = subprocess.run(
                COMPOSE + ["images", "--format", "json", SERVERS[engine]],
                capture_output=True,
                text=True,
                check=False,
            )
            manifest.setdefault("docker_images", {})[engine] = image.stdout.strip()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    write_jsonl(out / "sources.jsonl", read_jsonl(DATA / "sources.jsonl"))
    for engine in engines:
        try:
            if engine in SERVERS:
                subprocess.run(COMPOSE + ["up", "-d", SERVERS[engine]], check=True)
                wait_server(SERVERS[engine])
            with (out / f"{engine}.log").open("w") as log:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "main.py"),
                        "financebench",
                        "worker",
                        "--engine",
                        engine,
                        "--output",
                        str(out / engine),
                        "--repeats",
                        str(repeats),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            status.append(
                {
                    "engine": engine,
                    "status": "complete" if result.returncode == 0 else "failed",
                    "returncode": result.returncode,
                }
            )
            print(status[-1], flush=True)
        except Exception as exc:
            status.append({"engine": engine, "status": "failed", "error": str(exc)})
        finally:
            if engine in SERVERS:
                with (out / f"{engine}-server.log").open("w") as log:
                    subprocess.run(
                        COMPOSE + ["logs", SERVERS[engine]],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                subprocess.run(COMPOSE + ["stop", SERVERS[engine]], check=False)
            (out / "status.json").write_text(json.dumps(status, indent=2))
    from benchmarks.finance_report import report

    report(out)
    return 0 if all(s["status"] == "complete" for s in status) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "worker", "report"])
    parser.add_argument("--engine", choices=ENGINES + CLOUD_ENGINES, action="append")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("repeats must be positive")
    if args.action == "prepare":
        prepare()
    elif args.action == "worker":
        worker(args.engine[0], args.output, args.repeats)
    elif args.action == "report":
        from benchmarks.finance_report import report

        report(args.output)
    else:
        out = args.output or ROOT / "results/financebench" / datetime.now(
            timezone.utc
        ).strftime("run-%Y%m%dT%H%M%SZ")
        return run(out, args.engine or list(ENGINES), args.repeats)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
