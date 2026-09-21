"""English report and charts derived only from completed measurements."""

import json
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

from benchmarks.finance_data import DATA, read_jsonl

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def report(out):
    out = Path(out)
    status = json.loads((out / "status.json").read_text())
    has_cortex = any(s["engine"] == "cortex" for s in status)
    rows = []
    details = []
    for entry in status:
        if entry["status"] != "complete":
            continue
        engine = entry["engine"]
        directory = entry.get("directory", engine)
        meta = json.loads((out / directory / "metadata.json").read_text())
        meta["result_directory"] = directory
        details.append(meta)
        for mode in ["global", "document-filter"]:
            measurements = read_jsonl(out / directory / f"{mode}.jsonl")
            expected = (
                json.loads((out / "manifest.json").read_text())["questions"]
                * meta["repeats"]
            )
            if len(measurements) != expected:
                raise ValueError(f"Incomplete measurements: {engine} {mode}")
            frame = pd.DataFrame(measurements)
            ms = frame.latency_ms.to_numpy()
            rows.append(
                {
                    "engine": engine,
                    "mode": mode,
                    "requests": len(ms),
                    "p50_ms": np.percentile(ms, 50),
                    "p95_ms": np.percentile(ms, 95),
                    "p99_ms": np.percentile(ms, 99),
                    "mean_ms": ms.mean(),
                    "serial_qps": 1000 / ms.mean(),
                    "ann_recall": frame.ann_recall.mean(),
                    "tie_aware_ann_recall": frame.tie_aware_ann_recall.mean(),
                    "evidence_hit": frame.evidence_hit.mean(),
                    "evidence_page_recall": frame.evidence_page_recall.mean(),
                    "shortfall_count": int(frame.shortfall.sum()),
                    "load_and_index_s": meta["load_and_index_seconds"],
                }
            )
    summary = pd.DataFrame(rows)
    summary.to_csv(out / "summary.csv", index=False)
    manifest = json.loads((out / "manifest.json").read_text())
    pages = read_jsonl(DATA / "pages.jsonl")
    questions = read_jsonl(DATA / "questions.jsonl")
    chunks = read_jsonl(DATA / "chunks.jsonl")
    present_pages = {(c["doc_name"], c["page"]) for c in chunks}
    evidence_pages = {
        (e["doc_name"], e["evidence_page_num"])
        for q in questions
        for e in q["evidence"]
    }
    qa = {
        "pdf_pages": len(pages),
        "empty_text_pages": sum(p["chars"] == 0 for p in pages),
        "unique_evidence_pages": len(evidence_pages),
        "evidence_pages_without_chunks": sorted(evidence_pages - present_pages),
        "completed_engines": [r["engine"] for r in status if r["status"] == "complete"],
        "failed_engines": [r["engine"] for r in status if r["status"] != "complete"],
    }
    (out / "qa.json").write_text(json.dumps(qa, indent=2))
    if rows:
        fig, axes = plt.subplots(1, 2, figsize=(14, 7), layout="constrained")
        colors = {"global": "#3074b5", "document-filter": "#e59a32"}
        engines = list(summary.engine.unique())
        for mode, offset in [("global", -0.19), ("document-filter", 0.19)]:
            group = (
                summary[summary["mode"] == mode].set_index("engine").reindex(engines)
            )
            y = np.arange(len(engines)) + offset
            axes[0].scatter(group.p95_ms, y, color=colors[mode], label=mode, s=45)
            axes[1].barh(
                y,
                group.tie_aware_ann_recall * 100,
                height=0.36,
                color=colors[mode],
                label=mode,
            )
        for ax in axes:
            ax.set_yticks(np.arange(len(engines)), engines)
            ax.invert_yaxis()
            ax.grid(axis="x", alpha=0.2)
        axes[0].set_xscale("log")
        axes[0].set_xlabel("p95 latency (ms, log scale), lower is better")
        axes[1].set_xlabel("Tie-aware ANN recall@10 (%), higher is better")
        axes[1].set_xlim(0, 105)
        axes[0].legend(loc="lower right")
        fig.suptitle(
            f"FinanceBench | {manifest['chunk_count']:,} chunks | {manifest['questions']} queries | concurrency 1"
        )
        fig.savefig(out / "comparison.png", dpi=170)
        plt.close(fig)
    lines = [
        "# FinanceBench Vector Database Benchmark",
        "",
        "## Scope",
        "",
        f"Measured on {manifest['platform']}. {manifest['pdf_count']} PDFs, {len(pages):,} pages, {manifest['chunk_count']:,} chunks and all {manifest['questions']} public questions.",
        "This is a small-corpus, warm-cache, single-client experiment. It is not a million-vector, saturation, high-availability or production-cost benchmark.",
        "",
        "## Pipeline",
        "",
        "```mermaid",
        "flowchart LR",
        "  A[368 pinned FinanceBench PDFs] --> B[PyMuPDF page extraction]",
        "  B --> C[448-token chunks; 64-token overlap]",
        "  C --> D[BGE small English; normalized float32]",
        "  D --> E[Identical vectors in each database]",
        "  Q[150 questions] --> V[Shared query embeddings]",
        "  V --> E",
        "  D --> G[NumPy exact cosine ground truth]",
        "  E --> R[Latency; ANN recall; evidence-page retrieval]",
        "  G --> R",
        "```",
        "",
        "## Method",
        "",
        f"- Source commit: `{manifest['source_commit']}`; checksums: `sources.jsonl` and `manifest.json`.",
        f"- Embedding: `{manifest['model']}` revision `{manifest['model_revision']}`; 384 dimensions, normalized float32, identical query instruction for every DB.",
        f"- Parser: {manifest['parser']}. Page-local tokenizer chunks; tables are flattened text, not reconstructed tables.",
        "- All 368 PDFs are searched, not only the 84 documents referenced by questions. Identical/near-identical passages across years are retained as realistic distractors.",
        "- Top-10 cosine search. Global search and an oracle document filter (`doc_name` supplied by the benchmark) are separate modes. The latter is not evidence of automatic document selection.",
        "- OSS HNSW target settings: M=16, efConstruction=128, efSearch=128 where exposed, without configured quantization or reranking. This is a fixed-configuration comparison, not a recall-matched tuned ranking. The prefilter variants use exact search after filtering; their global search still uses HNSW. Cortex index internals and tuning are managed by Snowflake.",
        f"- Every query is warmed once per mode, then all questions run in seeded shuffled order for each of {manifest['repeats']} repetitions. Timings include request serialization and result IDs; exclude embedding, ground truth and scoring.",
        "- Server backends run sequentially in Docker (4 CPU / 8 GiB limit each). SQLite and DuckDB run natively on macOS; DuckDB uses 4 threads and in-memory tables. Network and virtualization differences prevent a pure engine-speed ranking.",
        f"- Reported serial QPS is 1000 / mean request latency, not measured maximum concurrent throughput. p99 is descriptive with only {manifest['questions'] * manifest['repeats']} requests per mode.",
        "- ANN recall uses exact top-10 chunk IDs; tie-aware recall counts unique returned candidates within 1e-5 cosine similarity of the exact tenth score. The tolerance addresses duplicated passages and floating-point ties; both are reported.",
        "- Evidence hit@10 is the fraction of questions with any annotated evidence page among returned chunks. Evidence-page recall averages coverage of all annotated pages. These are page-level proxies, not answer accuracy or exact evidence-span coverage.",
        "- Load/index time includes connection, schema creation, inserts and index preparation. These stages overlap in some engines, so the total is reported rather than misleading separate build times.",
        "",
        "## Results",
        "",
    ]
    if rows:
        lines += [
            "![Latency and recall](comparison.png)",
            "",
            "| Engine | Mode | p50 ms | p95 ms | ANN recall | Tie-aware recall | Evidence hit@10 | Shortfalls |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for r in rows:
            lines.append(
                f"| {r['engine']} | {r['mode']} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} | {r['ann_recall']:.1%} | {r['tie_aware_ann_recall']:.1%} | {r['evidence_hit']:.1%} | {r['shortfall_count']} |"
            )
    lines += [
        "",
        "## Coverage and limitations",
        "",
        f"Parsing QA: `{json.dumps(qa)}`",
        "",
        (
            "- Cortex Search uses supplied document/query vectors, a vector-only index, no embedding model, no text query and reranker=none. The Python API runs over HTTPS from the same Mac to AWS Seoul; these timings include WAN and SDK overhead, unlike localhost OSS timings. Shared embeddings make recall comparable, but cloud hardware and network differ. Snowflake SQL exact search and billed cloud credits were not measured."
            if has_cortex else
            "- Snowflake SQL and Cortex Search: not measured in this local run; credentials/configuration and a separate cloud-cost measurement are required."
        ),
        "- No OCR, layout reconstruction, embedding-model comparison, 1M/10M corpus, concurrency sweep, update/delete freshness, restart recovery, peak-memory or cloud-cost claims.",
        "- 150 public financial questions limit statistical power and domain generalization. Repetitions are latency samples, not 450 independent questions.",
        "- Timing order across engines is fixed; host background activity and different storage paths remain confounders. Repeat under Linux and matched resources before publication of general performance claims.",
        "",
        "### Engine status",
        "",
    ]
    lines += [
        f"- {r['engine']}: {r['status']}; results in `{r.get('directory', r['engine'])}`."
        for r in status
    ]
    lines += ["", "### Versions and plans", ""]
    for meta in details:
        lines += [
            f"- {meta['engine']}: {meta['version']}; load + index {meta['load_and_index_seconds']:.2f}s. Execution plans/settings in `{meta['result_directory']}/metadata.json`."
        ]
    lines += [
        "",
        "## Reproduce",
        "",
        "```bash",
        "uv sync --locked --extra vector",
        "uv run --extra vector python main.py financebench prepare",
        "docker compose -f docker-compose.vector.yml pull",
        "uv run --extra vector python main.py financebench run --repeats 3",
        "```",
        "",
        "Sources: [FinanceBench](https://github.com/patronus-ai/financebench), [embedding model](https://huggingface.co/BAAI/bge-small-en-v1.5).",
        "",
    ]
    body = "\n".join(lines)
    if has_cortex:
        body = body.replace(
            "- Server backends run sequentially in Docker",
            "- For local OSS comparison runs, server backends run sequentially in Docker",
        ).replace(
            "uv sync --locked --extra vector\nuv run --extra vector python main.py financebench prepare\ndocker compose -f docker-compose.vector.yml pull\nuv run --extra vector python main.py financebench run --repeats 3",
            "uv sync --locked --extra vector --extra snowflake\n# Requires the existing prepared data and benchmark Snowflake connection profile.\nuv run --extra vector --extra snowflake python main.py financebench run --engine cortex --repeats 3",
        )
    findings = out / "findings.md"
    if findings.exists():
        body = body.replace("## Pipeline", findings.read_text() + "\n\n## Pipeline", 1)
    (out / "report.md").write_text(body)
    print(f"Report: {out / 'report.md'}", flush=True)
