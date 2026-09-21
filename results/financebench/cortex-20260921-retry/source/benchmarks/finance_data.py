"""Pinned FinanceBench PDFs, page-preserving chunks and shared embeddings."""

import hashlib
import json
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/financebench"
COMMIT = "cc39aeb4afdf33909ee1412188bf89035950c2eb"
BASE = f"https://raw.githubusercontent.com/patronus-ai/financebench/{COMMIT}"
MODEL = "BAAI/bge-small-en-v1.5"
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def write_jsonl(path, rows):
    with Path(path).open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fetch(url, path):
    import requests

    response = requests.get(url, timeout=120)
    response.raise_for_status()
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_bytes(response.content)
    temp.replace(path)


def extract_pdf(source):
    import pymupdf

    name = Path(source["path"]).stem
    cache = DATA / "parsed" / f"{name}.json"
    if cache.exists():
        return name, json.loads(cache.read_text())
    with pymupdf.open(DATA / source["path"]) as pdf:
        texts = [page.get_text("text", sort=True).strip() for page in pdf]
    temp = cache.with_suffix(".part")
    temp.write_text(json.dumps(texts))
    temp.replace(cache)
    return name, texts


def prepare():
    import pymupdf
    import requests
    import torch
    from sentence_transformers import SentenceTransformer

    DATA.mkdir(parents=True, exist_ok=True)
    for name, source in [
        ("questions", "financebench_open_source"),
        ("documents", "financebench_document_information"),
    ]:
        fetch(f"{BASE}/data/{source}.jsonl", DATA / f"{name}.jsonl")
    tree_response = requests.get(
        f"https://api.github.com/repos/patronus-ai/financebench/git/trees/{COMMIT}",
        params={"recursive": 1},
        timeout=60,
    )
    tree_response.raise_for_status()
    tree = tree_response.json()
    pdfs = [r for r in tree["tree"] if r["path"].endswith(".pdf")]
    (DATA / "pdfs").mkdir(exist_ok=True)

    def download(row):
        path = DATA / row["path"]
        if not path.exists():
            fetch(f"{BASE}/{row['path']}", path)
        content = path.read_bytes()
        git_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if git_sha != row["sha"]:
            raise ValueError(f"Source checksum mismatch: {path}")
        return {"path": row["path"], "sha256": sha256(path), "bytes": len(content)}

    print(f"Downloading/verifying {len(pdfs)} PDFs", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        sources = list(pool.map(download, pdfs))
    write_jsonl(DATA / "sources.jsonl", sources)
    revision = MODEL_REVISION
    print(f"Loading {MODEL} revision {revision}", flush=True)
    torch.set_num_threads(4)
    model = SentenceTransformer(
        MODEL,
        revision=revision,
        cache_folder=str(DATA / "models"),
        device="mps" if torch.backends.mps.is_available() else "cpu",
    )
    tokenizer = model.tokenizer
    metadata = {r["doc_name"]: r for r in read_jsonl(DATA / "documents.jsonl")}
    chunks_path = DATA / "chunks.jsonl"
    pages_path = DATA / "pages.jsonl"
    if not chunks_path.exists():
        chunks, pages = [], []
        (DATA / "parsed").mkdir(exist_ok=True)
        with ProcessPoolExecutor(max_workers=6) as pool:
            for doc_index, (name, texts) in enumerate(pool.map(extract_pdf, sources)):
                if doc_index % 20 == 0:
                    print(
                        f"Parsed {doc_index}/{len(sources)} documents; {len(chunks)} chunks",
                        flush=True,
                    )
                for page_num, text in enumerate(texts):
                    pages.append(
                        {"doc_name": name, "page": page_num, "chars": len(text)}
                    )
                    tokens = tokenizer.encode(text, add_special_tokens=False)
                    for start in range(0, len(tokens), 384):
                        segment = tokens[start : start + 448]
                        if not segment:
                            continue
                        chunks.append(
                            {
                                "id": len(chunks),
                                "doc_name": name,
                                "page": page_num,
                                "token_start": start,
                                "text": tokenizer.decode(
                                    segment, skip_special_tokens=True
                                ),
                                "company": metadata.get(name, {}).get(
                                    "company", name.split("_")[0]
                                ),
                            }
                        )
                        if start + 448 >= len(tokens):
                            break
        write_jsonl(chunks_path, chunks)
        write_jsonl(pages_path, pages)
    chunks = read_jsonl(chunks_path)
    questions = read_jsonl(DATA / "questions.jsonl")
    print(f"Corpus: {len(chunks)} chunks; {len(questions)} questions", flush=True)
    started = time.time()
    for name, texts in [
        ("vectors", [c["text"] for c in chunks]),
        ("queries", [QUERY_PREFIX + q["question"] for q in questions]),
    ]:
        path = DATA / f"{name}.npy"
        if not path.exists():
            vectors = model.encode(
                texts,
                batch_size=64,
                normalize_embeddings=True,
                show_progress_bar=True,
                convert_to_numpy=True,
            )
            np.save(path, vectors.astype("float32"))
    manifest = {
        "source_commit": COMMIT,
        "model": MODEL,
        "model_revision": revision,
        "device": str(model.device),
        "chunk_tokens": 448,
        "overlap_tokens": 64,
        "parser": f"PyMuPDF {pymupdf.VersionBind}; get_text(text, sort=True); no OCR",
        "pdf_count": len(pdfs),
        "chunk_count": len(chunks),
        "questions": len(questions),
        "embedding_stage_seconds": time.time() - started,
        "query_prefix": QUERY_PREFIX,
        "hashes": {
            n: sha256(DATA / n)
            for n in ["chunks.jsonl", "vectors.npy", "queries.npy", "questions.jsonl"]
        },
    }
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2), flush=True)
