"""Doris preprocessing with the preserved Parquet inputs and integer semantics."""

from __future__ import annotations

import gc
import time
from pathlib import Path

import pandas as pd

from benchmarks.frame_workloads import fingerprint, sql_query

FACT_COLUMNS = (
    "id",
    "account_id",
    "region_id",
    "amount_cents",
    "quantity",
    "discount_pct",
    "event_day",
    "status",
)


def doris_query(workload: str, fact: str, dimension: str) -> str:
    query = sql_query(workload)
    if workload == "clean_derive":
        # Fixture amounts and discounts are nonnegative; FLOOR matches pandas //.
        query = """
            SELECT id, CAST(FLOOR(amount_cents * quantity *
                (100 - CAST(COALESCE(discount_pct, 0) AS BIGINT)) / 100)
                AS BIGINT) AS net_cents
            FROM sales
        """
    return query.replace("FROM sales", f"FROM {fact}").replace(
        "JOIN accounts", f"JOIN {dimension}"
    )


def normalize_result(rows: list | tuple, columns: list[str]) -> pd.DataFrame:
    """Every workload returns non-null int64 columns, including empty results."""
    return pd.DataFrame.from_records(
        rows, columns=[column.lower() for column in columns]
    ).astype("int64", copy=False)


class DorisBackend:
    def __init__(
        self,
        database: str,
        *,
        threads: int = 4,
        memory_gb: int = 8,
        timeout_seconds: int = 1800,
    ):
        from scripts.doris_client import connect

        self.database = database
        self.db = connect(database, create=True)
        self.tables: dict[int, str] = {}
        self.dimension = "accounts"
        self.setup_records: list[dict] = []
        try:
            self.command(f"SET parallel_pipeline_task_num={threads}")
            self.command(f"SET exec_mem_limit={memory_gb * 2**30}")
            self.command(f"SET query_timeout={timeout_seconds}")
            self.command("SET enable_sql_cache=false")
            self.command("SET enable_query_cache=false")
            self.metadata = self.db.metadata()
            self.metadata.update(
                {
                    "database": database,
                    "parallel_pipeline_task_num": threads,
                    "exec_mem_limit": memory_gb * 2**30,
                    "enable_sql_cache": False,
                    "enable_query_cache": False,
                    "client_memory_is_not_server_memory": True,
                }
            )
        except Exception:
            self.close()
            raise

    def command(self, sql: str) -> list | tuple:
        cursor = self.db.execute(sql)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def load(self, dataset: dict) -> None:
        if not self.tables:
            self._load_table(
                Path(dataset["dimension"]),
                self.dimension,
                ("account_id", "tier"),
                dataset["dimension_rows"],
            )
        rows = dataset["rows"]
        table = f"sales_{rows}"
        self._load_table(Path(dataset["fact"]), table, FACT_COLUMNS, rows)
        self.tables[rows] = table

    def _load_table(
        self, path: Path, table: str, columns: tuple, expected_rows: int
    ) -> None:
        definitions = ", ".join(
            f"`{column}` DOUBLE NULL"
            if column == "discount_pct"
            else f"`{column}` BIGINT NOT NULL"
            for column in columns
        )
        started = time.perf_counter()
        self.command(
            f"CREATE TABLE {table} ({definitions}) "
            f"DUPLICATE KEY({columns[0]}) "
            f"DISTRIBUTED BY HASH({columns[0]}) BUCKETS 8 "
            'PROPERTIES ("replication_num"="1")'
        )
        ddl_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        loaded = self.db.stream_load(
            table, path, format="parquet", columns=",".join(columns)
        )
        self.db.commit()
        load_ms = (time.perf_counter() - started) * 1000
        actual_rows = int(self.command(f"SELECT COUNT(*) FROM {table}")[0][0])
        if actual_rows != expected_rows:
            raise ValueError(
                f"Doris row count mismatch: {actual_rows} != {expected_rows}"
            )
        self.setup_records.append(
            {
                "file": str(path),
                "table": table,
                "rows": actual_rows,
                "ddl_ms": ddl_ms,
                "stream_load_ms": load_ms,
                "stream_load": loaded,
            }
        )

    def _execute(self, query: str) -> tuple[pd.DataFrame, dict]:
        cpu_started = time.process_time_ns()
        started = time.perf_counter_ns()
        cursor = self.db.execute(query)
        executed = time.perf_counter_ns()
        try:
            rows = cursor.fetchall()
            columns = [item[0] for item in cursor.description]
            output = normalize_result(rows, columns)
            finished = time.perf_counter_ns()
        finally:
            cursor.close()
        return output, {
            "elapsed_ms": (finished - started) / 1e6,
            "execute_roundtrip_ms": (executed - started) / 1e6,
            "fetch_dataframe_ms": (finished - executed) / 1e6,
            "cpu_ms": (time.process_time_ns() - cpu_started) / 1e6,
            "output_rows": len(output),
        }

    def run(
        self, case: dict, runs: int, expected: dict, *, profile: bool = False
    ) -> dict:
        query = doris_query(case["workload"], self.tables[case["rows"]], self.dimension)
        output, _ = self._execute(query)
        if fingerprint(output) != expected:
            raise ValueError(f"Doris result differs from historical reference: {case}")
        if profile:
            return {
                "fingerprint": expected,
                "import_rss_bytes": None,
                "setup_rss_bytes": None,
                "process_peak_rss_bytes": None,
                "output_bytes": int(output.memory_usage(index=True, deep=True).sum()),
            }
        del output
        measurements = []
        for _ in range(runs):
            gc.collect()
            output, sample = self._execute(query)
            if fingerprint(output) != expected:
                raise ValueError(
                    f"Doris repeated result differs from reference: {case}"
                )
            sample["validated"] = True
            measurements.append(sample)
            del output
        return {"fingerprint": expected, "measurements": measurements}

    def close(self) -> None:
        try:
            self.command(f"DROP DATABASE IF EXISTS {self.database}")
        finally:
            self.db.close()
