"""Identical numeric transformations, with explicit output and null semantics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

WORKLOADS = {
    "filter_project": ["id", "region_id", "amount_cents", "quantity"],
    "clean_derive": ["id", "amount_cents", "quantity", "discount_pct"],
    "groupby": ["region_id", "amount_cents", "quantity"],
    "join_groupby": ["account_id", "region_id", "amount_cents"],
}
LABELS = {
    "filter_project": "Filter + derive",
    "clean_derive": "Fill nulls + derive",
    "groupby": "Group by region",
    "join_groupby": "Join + group by",
}
FILTERS = [("region_id", "<", 10), ("amount_cents", ">=", 1000)]
DATA_VERSION = 1
ACCOUNTS = 100_000


def make_batch(start: int, count: int, rng: np.random.Generator) -> pd.DataFrame:
    discount = rng.integers(0, 31, count).astype("float64")
    discount[rng.random(count) < 0.05] = np.nan
    return pd.DataFrame(
        {
            "id": np.arange(start, start + count, dtype="int64"),
            "account_id": rng.integers(0, ACCOUNTS, count, dtype="int64"),
            "region_id": rng.integers(0, 50, count, dtype="int64"),
            "amount_cents": rng.integers(0, 100_001, count, dtype="int64"),
            "quantity": rng.integers(1, 11, count, dtype="int64"),
            "discount_pct": discount,
            "event_day": rng.integers(0, 365, count, dtype="int64"),
            "status": rng.integers(0, 4, count, dtype="int64"),
        }
    )


def generate_dataset(root: Path, rows: int, seed: int) -> dict:
    folder = root / f"v{DATA_VERSION}-seed{seed}-rows{rows}"
    folder.mkdir(parents=True, exist_ok=True)
    fact, dim = folder / "sales.parquet", folder / "accounts.parquet"
    metadata_path = folder / "dataset.json"
    if metadata_path.exists() and fact.exists() and dim.exists():
        metadata = json.loads(metadata_path.read_text())
        for path in (fact, dim):
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest != metadata["sha256"][path.name]:
                raise ValueError(f"Dataset checksum mismatch: {path}")
        return metadata
    rng = np.random.default_rng(seed)
    writer = None
    try:
        for start in range(0, rows, 500_000):
            frame = make_batch(start, min(500_000, rows - start), rng)
            table = pa.Table.from_pandas(frame, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(fact, table.schema, compression="snappy")
            writer.write_table(table, row_group_size=100_000)
    finally:
        if writer is not None:
            writer.close()
    accounts = np.arange(ACCOUNTS, dtype="int64")
    pd.DataFrame({"account_id": accounts, "tier": accounts % 4}).to_parquet(
        dim, engine="pyarrow", compression="snappy", index=False
    )
    hashes = {}
    for path in (fact, dim):
        with path.open("rb") as handle:
            hashes[path.name] = hashlib.file_digest(handle, "sha256").hexdigest()
    metadata = {
        "version": DATA_VERSION,
        "seed": seed,
        "rows": rows,
        "columns": 8,
        "logical_fact_bytes": rows * 8 * 8,
        "parquet_fact_bytes": fact.stat().st_size,
        "dimension_rows": ACCOUNTS,
        "fact": str(fact.resolve()),
        "dimension": str(dim.resolve()),
        "sha256": hashes,
        "compression": "snappy",
        "row_group_size": 100_000,
        "distribution": "Independent uniform numeric columns; 5% missing discounts",
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))
    return metadata


def read_input(fact: Path, workload: str, *, pushdown: bool) -> pd.DataFrame:
    filters = FILTERS if pushdown and workload == "filter_project" else None
    return pd.read_parquet(
        fact, engine="pyarrow", columns=WORKLOADS[workload], filters=filters
    )


def pandas_transform(
    frame: pd.DataFrame, accounts: pd.DataFrame | None, workload: str
) -> pd.DataFrame:
    if workload == "filter_project":
        selected = frame.loc[(frame.region_id < 10) & (frame.amount_cents >= 1000)]
        return pd.DataFrame(
            {
                "id": selected.id,
                "region_id": selected.region_id,
                "gross_cents": selected.amount_cents * selected.quantity,
            }
        ).reset_index(drop=True)
    if workload == "clean_derive":
        discount = frame.discount_pct.fillna(0).astype("int64")
        return pd.DataFrame(
            {
                "id": frame.id,
                "net_cents": frame.amount_cents
                * frame.quantity
                * (100 - discount)
                // 100,
            }
        )
    if workload == "groupby":
        return frame.groupby("region_id", sort=False, as_index=False).agg(
            total_cents=("amount_cents", "sum"),
            total_quantity=("quantity", "sum"),
            row_count=("amount_cents", "size"),
        )
    if workload == "join_groupby":
        joined = frame.merge(accounts, on="account_id", how="inner", sort=False)
        return joined.groupby(["region_id", "tier"], sort=False, as_index=False).agg(
            total_cents=("amount_cents", "sum"),
            row_count=("amount_cents", "size"),
        )
    raise ValueError(workload)


def sql_query(workload: str) -> str:
    queries = {
        "filter_project": """
            SELECT id, region_id, amount_cents * quantity AS gross_cents
            FROM sales WHERE region_id < 10 AND amount_cents >= 1000
        """,
        "clean_derive": """
            SELECT id, amount_cents * quantity *
                (100 - CAST(COALESCE(discount_pct, 0) AS BIGINT)) // 100 AS net_cents
            FROM sales
        """,
        "groupby": """
            SELECT region_id, CAST(SUM(amount_cents) AS BIGINT) AS total_cents,
                CAST(SUM(quantity) AS BIGINT) AS total_quantity, COUNT(*) AS row_count
            FROM sales GROUP BY region_id
        """,
        "join_groupby": """
            SELECT region_id, tier, CAST(SUM(amount_cents) AS BIGINT) AS total_cents,
                COUNT(*) AS row_count
            FROM sales JOIN accounts USING (account_id) GROUP BY region_id, tier
        """,
    }
    return queries[workload]


def fingerprint(frame: pd.DataFrame) -> dict:
    """Order-independent full-result row hashes; never included in elapsed time."""
    values = pd.util.hash_pandas_object(frame, index=False).to_numpy()
    return {
        "rows": len(frame),
        "columns": list(frame.columns),
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "hash_sum": str(int(values.sum(dtype=np.uint64))),
        "hash_xor": str(int(np.bitwise_xor.reduce(values, initial=np.uint64(0)))),
    }
