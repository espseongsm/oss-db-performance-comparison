"""Independently validate raw results; run from repository root after report generation."""

import hashlib
import json
import random
from pathlib import Path

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

threadpool_limits(4)
out = Path(__file__).resolve().parent
root = out.parents[2]
data = root / "data/financebench"
manifest = json.loads((out / "manifest.json").read_text())
for name, expected in manifest["hashes"].items():
    assert hashlib.sha256((data / name).read_bytes()).hexdigest() == expected
vectors = np.load(data / "vectors.npy")
queries = np.load(data / "queries.npy")
chunks = [json.loads(line) for line in (data / "chunks.jsonl").open()]
questions = [json.loads(line) for line in (data / "questions.jsonl").open()]
reference = {}
for mode in ["global", "document-filter"]:
    for qi, (q, v) in enumerate(zip(questions, queries)):
        eligible = np.array(
            [
                i
                for i, c in enumerate(chunks)
                if mode == "global" or c["doc_name"] == q["doc_name"]
            ]
        )
        scores = vectors @ v
        best = eligible[np.lexsort((eligible, -scores[eligible]))[:10]]
        reference[mode, qi] = (set(best), scores, float(scores[best[-1]]))
summary = pd.read_csv(out / "summary.csv")
checks = []
for entry in json.loads((out / "status.json").read_text()):
    assert entry["status"] == "complete"
    directory = out / entry["directory"]
    meta = json.loads((directory / "metadata.json").read_text())
    for mode in ["global", "document-filter"]:
        rows = [json.loads(line) for line in (directory / f"{mode}.jsonl").open()]
        matrix = {(r["repeat"], r["question_index"]) for r in rows}
        expected = {
            (r, q) for r in range(meta["repeats"]) for q in range(len(questions))
        }
        assert len(rows) == len(matrix) == len(expected) and matrix == expected
        for repeat in range(meta["repeats"]):
            order = list(range(len(questions)))
            random.Random(20260921 + repeat).shuffle(order)
            assert [r["question_index"] for r in rows if r["repeat"] == repeat] == order
        for row in rows:
            q = questions[row["question_index"]]
            ids = row["ids"]
            assert row["engine"] == entry["engine"] and row["mode"] == mode
            assert row["question_id"] == q["financebench_id"]
            assert len(ids) == len(set(ids)) and len(ids) <= 10
            assert all(0 <= i < len(chunks) for i in ids)
            assert row["latency_ms"] > 0 and np.isfinite(row["latency_ms"])
            if mode == "document-filter":
                assert all(chunks[i]["doc_name"] == q["doc_name"] for i in ids)
            gold, scores, cutoff = reference[mode, row["question_index"]]
            evidence = {(e["doc_name"], e["evidence_page_num"]) for e in q["evidence"]}
            retrieved = {(chunks[i]["doc_name"], chunks[i]["page"]) for i in ids}
            computed = {
                "ann_recall": len(set(ids) & gold) / len(gold),
                "tie_aware_ann_recall": min(
                    len(gold), sum(scores[i] >= cutoff - 1e-5 for i in ids)
                )
                / len(gold),
                "evidence_hit": int(bool(evidence & retrieved)),
                "evidence_page_recall": len(evidence & retrieved) / len(evidence),
                "shortfall": int(len(ids) < len(gold)),
            }
            for name, value in computed.items():
                assert abs(row[name] - value) < 1e-12, (
                    entry["engine"],
                    mode,
                    row["question_index"],
                    name,
                )
        agg = summary[
            (summary.engine == entry["engine"]) & (summary["mode"] == mode)
        ].iloc[0]
        ms = np.array([r["latency_ms"] for r in rows])
        for pct in [50, 95, 99]:
            assert abs(agg[f"p{pct}_ms"] - np.percentile(ms, pct)) < 1e-8
        for name in [
            "ann_recall",
            "tie_aware_ann_recall",
            "evidence_hit",
            "evidence_page_recall",
        ]:
            assert abs(agg[name] - np.mean([r[name] for r in rows])) < 1e-12
        checks.append(
            {"engine": entry["engine"], "mode": mode, "requests_verified": len(rows)}
        )
cortex = out / "../cortex-20260921-measured/cortex"
plan = json.loads((cortex / "cloud-plan.json").read_text())
cleanup = json.loads((cortex / "cleanup.json").read_text())
assert plan["verified_vector_rows"] == len(chunks)
assert plan["embedding_generation"] is False
assert cleanup["service_and_source_removed"] and cleanup["database_dropped"]
result = {
    "passed": True,
    "measured_requests_verified": sum(c["requests_verified"] for c in checks),
    "input_hashes_verified": manifest["hashes"],
    "checks": checks,
    "cortex_all_uploaded_vectors_verified": len(chunks),
    "cortex_cleanup_record_verified": True,
}
(out / "audit.json").write_text(json.dumps(result, indent=2))
print(
    json.dumps(
        {
            "passed": True,
            "measured_requests_verified": result["measured_requests_verified"],
        }
    )
)
