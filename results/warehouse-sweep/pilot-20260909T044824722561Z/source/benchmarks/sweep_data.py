"""Reuse canonical input and make parallel-load Parquet parts without changing rows."""

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq

from benchmarks.frame_workloads import generate_dataset


def prepare(root, rows, seed):
    dataset = generate_dataset(root, rows, seed)
    if rows <= 10_000_000:
        return dataset
    folder = Path(dataset["fact"]).parent / "snowflake-parts"
    folder.mkdir(exist_ok=True)
    marker = folder / "parts.json"
    if marker.exists():
        metadata = json.loads(marker.read_text())
        assert metadata["source_sha256"] == dataset["sha256"]["sales.parquet"]
        for part in metadata["parts"]:
            with (folder / part["file"]).open("rb") as handle:
                assert (
                    hashlib.file_digest(handle, "sha256").hexdigest() == part["sha256"]
                )
        assert sum(p["rows"] for p in metadata["parts"]) == rows
    else:
        source = pq.ParquetFile(dataset["fact"])
        parts, writer, count, path = [], None, 0, None
        try:
            for index, batch in enumerate(source.iter_batches(batch_size=500_000)):
                if index % 20 == 0:
                    if writer is not None:
                        writer.close()
                        parts.append({"file": path.name, "rows": count})
                    path = folder / f"part-{index // 20:04d}.parquet"
                    writer = pq.ParquetWriter(
                        path, source.schema_arrow, compression="snappy"
                    )
                    count = 0
                writer.write_batch(batch, row_group_size=100_000)
                count += batch.num_rows
        finally:
            if writer is not None:
                writer.close()
        parts.append({"file": path.name, "rows": count})
        assert sum(p["rows"] for p in parts) == rows
        for part in parts:
            file = folder / part["file"]
            assert pq.ParquetFile(file).metadata.num_rows == part["rows"]
            with file.open("rb") as handle:
                part["sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
        metadata = {"source_sha256": dataset["sha256"]["sales.parquet"], "parts": parts}
        marker.write_text(json.dumps(metadata, indent=2))
    return {**dataset, "load_fact": str(folder), "load_parts": metadata}
