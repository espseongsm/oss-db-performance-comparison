import numpy as np
import pytest

from benchmarks.finance_cortex import vector_query
from benchmarks.financebench import assess, exact_truth


def test_cortex_uses_only_existing_vector_and_preserves_filter():
    vector = np.array([0.25, -0.75], dtype=np.float32)
    request = vector_query(vector, "Company's report")
    assert "query" not in request
    assert request["multi_index_query"] == {"EMBEDDING": [{"vector": [0.25, -0.75]}]}
    assert request["scoring_config"]["reranker"] == "none"
    assert request["scoring_config"]["weights"] == {
        "texts": 0,
        "vectors": 1,
        "reranker": 0,
    }
    assert request["filter"] == {"@eq": {"DOC_NAME": "Company's report"}}
    assert "filter" not in vector_query(vector)


def test_filter_changes_exact_ground_truth():
    vectors = np.array([[1.0, 0.0], [0.8, 0.6], [0.0, 1.0]])
    chunks = [
        {"doc_name": "a", "page": 0},
        {"doc_name": "b", "page": 1},
        {"doc_name": "b", "page": 2},
    ]
    ids, cutoff, scores = exact_truth(vectors, vectors[0], chunks, "b", k=1)
    assert ids == [1]
    q = {"evidence": [{"doc_name": "b", "evidence_page_num": 1}]}
    assert assess([1], ids, cutoff, scores, chunks, q, "b")["evidence_hit"] == 1
    with pytest.raises(ValueError, match="filter"):
        assess([0], ids, cutoff, scores, chunks, q, "b")


def test_duplicate_vectors_do_not_penalize_tie_aware_recall():
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    chunks = [{"doc_name": "a", "page": i} for i in range(3)]
    truth = exact_truth(vectors, vectors[0], chunks, k=1)
    q = {"evidence": [{"doc_name": "a", "evidence_page_num": 0}]}
    result = assess([1], *truth, chunks, q)
    assert result["ann_recall"] == 0
    assert result["tie_aware_ann_recall"] == 1
    assert result["evidence_hit"] == 0
    with pytest.raises(ValueError, match="duplicate"):
        assess([1, 1], *truth, chunks, q)


def test_page_coverage_and_shortfall():
    vectors = np.eye(3)
    chunks = [{"doc_name": "a", "page": i} for i in range(3)]
    q = {"evidence": [{"doc_name": "a", "evidence_page_num": i} for i in [0, 1]]}
    result = assess([0], *exact_truth(vectors, vectors[0], chunks), chunks, q)
    assert result["shortfall"] == 1
    assert result["evidence_page_recall"] == 0.5


@pytest.mark.parametrize("fail_metadata", [False, True])
def test_worker_serializes_cloud_dates_and_always_closes(
    tmp_path, monkeypatch, fail_metadata
):
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace

    from benchmarks import financebench

    data = tmp_path / "data"
    data.mkdir()
    np.save(data / "vectors.npy", np.array([[1.0, 0.0]]))
    np.save(data / "queries.npy", np.array([[1.0, 0.0]]))
    (data / "chunks.jsonl").write_text(
        json.dumps({"id": 0, "doc_name": "a", "page": 0}) + "\n"
    )
    question = {
        "financebench_id": "q",
        "doc_name": "a",
        "evidence": [{"doc_name": "a", "evidence_page_num": 0}],
    }
    (data / "questions.jsonl").write_text(json.dumps(question) + "\n")
    closed = []
    db = SimpleNamespace(
        version="test",
        plan={"date": datetime(2026, 9, 21, tzinfo=timezone.utc)},
        search=lambda *_: [0],
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(financebench, "DATA", data)
    monkeypatch.setattr(financebench, "backend", lambda *_: db)
    out = tmp_path / "out"
    if fail_metadata:
        original = Path.write_text

        def write_text(path, *args, **kwargs):
            if path.name == "metadata.json":
                raise OSError("metadata write failed")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
        with pytest.raises(OSError, match="metadata write failed"):
            financebench.worker("cortex", out, 1)
    else:
        financebench.worker("cortex", out, 1)
        assert json.loads((out / "metadata.json").read_text())["plan"][
            "date"
        ].startswith("2026-09-21")
    assert closed == [True]
