"""Verify saved source/input hashes and load records without rerunning workloads."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def check_ids(path, expected_rows, start=0, expected_columns=None):
    metadata = pq.ParquetFile(path).metadata
    assert metadata.num_rows == expected_rows, str(path)
    if expected_columns is not None:
        assert metadata.num_columns == expected_columns, str(path)
    next_id = start
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        values = group.column(0).statistics
        assert values.min == next_id, (str(path), index)
        assert values.max == next_id + group.num_rows - 1, (str(path), index)
        next_id += group.num_rows
    return next_id


def main():
    folder = Path(__file__).resolve().parent
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    sources = manifest["source_sha256"]
    for name, expected in sources.items():
        assert digest(folder / "source" / name) == expected, name
    datasets = json.loads((folder / "datasets.json").read_text())
    inputs, parts, dimensions = [], [], []
    for dataset in datasets:
        for key in ("fact", "dimension"):
            path = Path(dataset[key])
            expected = dataset["sha256"][path.name]
            assert digest(path) == expected, str(path)
            inputs.append({"path": str(path), "sha256": expected})
        check_ids(Path(dataset["fact"]), dataset["rows"], expected_columns=8)
        dimension = pq.read_table(dataset["dimension"])
        ids = dimension["account_id"].to_pylist()
        tiers = dimension["tier"].to_pylist()
        assert ids == list(range(100_000))
        assert tiers == [value % 4 for value in ids]
        dimensions.append(dataset["rows"])
        if "load_parts" in dataset:
            metadata = dataset["load_parts"]
            assert metadata["source_sha256"] == dataset["sha256"]["sales.parquet"]
            load_folder = Path(dataset["load_fact"])
            assert json.loads((load_folder / "parts.json").read_text()) == metadata
            next_id = 0
            for part in metadata["parts"]:
                path = load_folder / part["file"]
                assert digest(path) == part["sha256"], str(path)
                next_id = check_ids(path, part["rows"], next_id, 8)
                parts.append({"path": str(path), "sha256": part["sha256"]})
            assert next_id == dataset["rows"]
    loads = json.loads((folder / "snowflake-setup.json").read_text())
    assert len(loads) == 6
    assert sorted(item["rows"] for item in loads) == sorted(
        [100_000, *manifest["settings"]["sizes"]]
    )
    for item in loads:
        assert all(row[6] == "UPLOADED" for row in item["put_result"])
        assert all(row[1] == "LOADED" and row[5] == 0 for row in item["copy_result"])
        assert sum(row[2] for row in item["copy_result"]) == item["rows"]
        assert sum(row[3] for row in item["copy_result"]) == item["rows"]
    for warehouse in manifest["warehouses"].values():
        assert warehouse["type"] == "STANDARD" and str(warehouse["generation"]) == "2"
        assert warehouse["min_cluster_count"] == warehouse["max_cluster_count"] == 1
        assert warehouse["auto_suspend"] == 60
    assert manifest["snowflake_session"]["use_cached_result"] is False
    result = {
        "status": "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshots_verified": len(sources),
        "canonical_input_files_verified": len(inputs),
        "load_part_files_verified": len(parts),
        "canonical_rows": [item["rows"] for item in datasets],
        "row_count_and_contiguous_id_footers": "passed for all facts and parts",
        "dimension_contents": "all five files contain account_id 0..99999 and tier=account_id%4",
        "successful_load_records": len(loads),
        "warehouse_manifest": "four Standard Gen2 single-cluster sizes; result reuse disabled",
        "input_hashes": inputs,
        "part_hashes": parts,
        "limitations": "File hashes/footer checks do not independently recompute the full input/output dataset. Warmup output checksums are audited separately.",
    }
    (folder / "provenance-audit.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({key: value for key, value in result.items() if not key.endswith("hashes")}))


if __name__ == "__main__":
    main()
