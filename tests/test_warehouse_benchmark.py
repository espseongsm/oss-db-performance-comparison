"""Offline checks for the embedded handler and evidence completeness gates."""

import itertools
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest

from benchmarks.frame_workloads import (
    WORKLOADS,
    fingerprint,
    make_batch,
    pandas_transform,
)
from benchmarks.warehouse_benchmark import ENGINES, procedure_source
from benchmarks.warehouse_report import audit


class History:
    def __enter__(self):
        self.queries = []
        return self

    def __exit__(self, *args):
        pass


class Session:
    def __init__(self, connection):
        self.connection = connection
        self.history = History()

    def query_history(self):
        return self.history

    def sql(self, query):
        self.history.queries.append(
            SimpleNamespace(query_id=str(len(self.history.queries)))
        )
        frame = self.connection.sql(query).df()
        frame.columns = [c.upper() for c in frame.columns]
        return SimpleNamespace(to_pandas=lambda: frame)


@pytest.mark.parametrize(
    "workload,engine,empty", list(itertools.product(WORKLOADS, ENGINES, (False, True)))
)
def test_embedded_handler_roundtrip(workload, engine, empty):
    namespace = {}
    exec(compile(procedure_source(), "procedure.py", "exec"), namespace)
    frame = make_batch(0, 500, np.random.default_rng(12))
    if empty:
        frame = frame.iloc[:0]
    accounts = pd.DataFrame(
        {"account_id": range(100_000), "tier": np.arange(100_000) % 4}
    )
    expected = fingerprint(pandas_transform(frame, accounts, workload))
    config = dict(
        engine=engine,
        workload=workload,
        fact="sf_fact",
        dimension="sf_accounts",
        threads=1,
        memory_gb=1,
        expected=expected,
    )
    with duckdb.connect() as connection:
        connection.register("sf_fact", frame)
        connection.register("sf_accounts", accounts)
        result = namespace["sample"](Session(connection), config)
    assert result["fingerprint"] == expected
    assert result["validated"] and result["query_ids"]
    assert result["elapsed_ms"] == pytest.approx(
        (result["input_ms"] or 0)
        + result["setup_ms"]
        + result["compute_to_dataframe_ms"]
    )
    assert (result["input_ms"] is None) == (engine == "snowflake")


def evidence():
    reference = fingerprint(pd.DataFrame({"id": [1]}))
    records = []
    for work, engine, run in itertools.product(WORKLOADS, ENGINES, (1, 2)):
        records.append(
            dict(
                rows=1000,
                workload=work,
                engine=engine,
                run=run,
                block=1,
                elapsed_ms=float(run),
                input_ms=None if engine == "snowflake" else 0.5,
                setup_ms=0.0,
                compute_to_dataframe_ms=run - (0 if engine == "snowflake" else 0.5),
                fingerprint=reference,
                validated=True,
                output_rows=1,
            )
        )
    return (
        pd.DataFrame(records),
        {"settings": dict(sizes=[1000], runs=2, blocks=1)},
        {f"1000/{w}": reference for w in WORKLOADS},
    )


def test_report_statistics():
    summary = audit(*evidence())
    assert len(summary) == 12
    assert summary.mean_ms.eq(1.5).all()
    assert summary.std_ms.tolist() == pytest.approx([2**-0.5] * 12)


@pytest.mark.parametrize(
    "fault", ("missing_condition", "duplicate", "fingerprint", "components", "block")
)
def test_report_rejects_untrustworthy_evidence(fault):
    raw, manifest, references = evidence()
    if fault == "missing_condition":
        raw = raw.loc[raw.engine != "pandas"]
    elif fault == "duplicate":
        raw = pd.concat([raw, raw.iloc[:1]])
    elif fault == "fingerprint":
        raw.at[0, "fingerprint"] = {"rows": 99}
    elif fault == "components":
        raw.at[0, "elapsed_ms"] = 42
    else:
        raw.at[0, "block"] = 2
    with pytest.raises(ValueError):
        audit(raw, manifest, references)
