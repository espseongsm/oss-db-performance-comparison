"""Descriptive statistics and uncertainty that preserves measurement blocks."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

KEYS = ["rows", "mode", "workload"]


def summarize(raw: pd.DataFrame, memory: pd.DataFrame) -> pd.DataFrame:
    records = []
    for key, group in raw.groupby([*KEYS, "engine"], sort=True):
        times = group.elapsed_ms.to_numpy()
        mean = float(times.mean())
        std = float(times.std(ddof=1)) if len(times) > 1 else None
        q1, median, q3, p90, p95, p99 = np.quantile(
            times, [0.25, 0.5, 0.75, 0.9, 0.95, 0.99]
        )
        block_means = group.groupby("block").elapsed_ms.mean().to_numpy()
        margin = None
        if len(block_means) > 1:
            margin = float(
                stats.t.ppf(0.975, len(block_means) - 1) * stats.sem(block_means)
            )
        record = dict(zip([*KEYS, "engine"], key, strict=True))
        record.update(
            {
                "n": len(times),
                "blocks": len(block_means),
                "mean_ms": mean,
                "std_ms": std,
                "min_ms": float(times.min()),
                "max_ms": float(times.max()),
                "median_ms": float(median),
                "q1_ms": float(q1),
                "q3_ms": float(q3),
                "iqr_ms": float(q3 - q1),
                "p90_ms": float(p90),
                "p95_ms": float(p95),
                "p99_ms": float(p99),
                "mad_ms": float(np.median(np.abs(times - median))),
                "cv_pct": std / mean * 100 if std is not None else None,
                "mean_ci95_low_ms": max(0, mean - margin)
                if margin is not None
                else None,
                "mean_ci95_high_ms": mean + margin if margin is not None else None,
                "input_million_rows_per_s": key[0] / mean / 1000,
                "mean_cpu_ms": float(group.cpu_ms.mean()),
                "output_rows": int(group.output_rows.iloc[0]),
                "tukey_outlier_count": int(
                    (
                        (times < q1 - 1.5 * (q3 - q1)) | (times > q3 + 1.5 * (q3 - q1))
                    ).sum()
                ),
            }
        )
        records.append(record)
    summary = pd.DataFrame(records)
    return summary.merge(memory, on=[*KEYS, "engine"], validate="one_to_one")


def compare(raw: pd.DataFrame, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    records = []
    for key, group in raw.groupby(KEYS, sort=True):
        blocks = group.groupby(["block", "engine"]).elapsed_ms.mean().unstack()
        if blocks[["pandas", "duckdb"]].isna().any().any():
            raise ValueError("Incomplete engine pairs")
        pandas_ms = blocks.pandas.to_numpy()
        duckdb_ms = blocks.duckdb.to_numpy()
        ratio = float(pandas_ms.mean() / duckdb_ms.mean())
        low = high = None
        if len(blocks) > 1:
            indexes = rng.integers(0, len(blocks), size=(10_000, len(blocks)))
            samples = pandas_ms[indexes].mean(axis=1) / duckdb_ms[indexes].mean(axis=1)
            low, high = (float(x) for x in np.quantile(samples, [0.025, 0.975]))
        record = dict(zip(KEYS, key, strict=True))
        record.update(
            {
                "pandas_mean_ms": float(pandas_ms.mean()),
                "duckdb_mean_ms": float(duckdb_ms.mean()),
                "pandas_over_duckdb": ratio,
                "ratio_ci95_low": low,
                "ratio_ci95_high": high,
                "observed_faster": "duckdb" if ratio > 1 else "pandas",
                "paired_blocks": len(blocks),
            }
        )
        records.append(record)
    return pd.DataFrame(records)
