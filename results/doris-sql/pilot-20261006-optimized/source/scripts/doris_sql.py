"""Doris physical design and deterministic SQL benchmark loading."""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

FACT_COLUMNS = (
    "id",
    "account_id",
    "category_id",
    "region_id",
    "quantity",
    "amount",
    "discount",
    "event_day",
    "event_second",
    "is_active",
    "status_id",
    "source_id",
    *(f"metric_{number:02d}" for number in range(1, 9)),
)
DIM_COLUMNS = ("account_id", "account_tier", "account_region_id")


def verify_reference(validation: dict[str, Any], path: Path) -> dict[str, Any]:
    reference = json.loads(path.read_text())
    result = {"source": str(path), "reference_rows": reference["rows"]}
    if reference["rows"] != validation["rows"]:
        return {**result, "status": "skipped_rows_mismatch"}
    for query, actual in validation["queries"].items():
        expected = reference["queries"][query]
        if any(actual[key] != expected[key] for key in ("row_count", "digest")):
            raise RuntimeError(
                f"doris/{query}: 기준 DuckDB 결과와 checksum 또는 행 수가 다릅니다."
            )
    return {**result, "status": "passed"}


def connect():
    if __package__:
        from .doris_client import connect as client_connect
    else:
        from doris_client import connect as client_connect

    return client_connect(database="benchmark_sql", create=True)


def ddl(table: str, optimized: bool) -> str:
    columns = list(FACT_COLUMNS)
    keys = "id"
    bloom = ""
    if optimized:
        columns = [
            "event_day",
            "id",
            *(c for c in columns if c not in ("event_day", "id")),
        ]
        keys = "event_day, id"
        bloom = ', "bloom_filter_columns" = "id"'
    definitions = ", ".join(
        f"{column} {'DOUBLE' if column in ('amount', 'discount') else 'BIGINT'}"
        for column in columns
    )
    return (
        f"CREATE TABLE {table} ({definitions}) DUPLICATE KEY({keys}) "
        "DISTRIBUTED BY HASH(id) BUCKETS 8 "
        f'PROPERTIES ("replication_num" = "1"{bloom})'
    )


def dimension_ddl(table: str) -> str:
    definitions = ", ".join(f"{column} BIGINT" for column in DIM_COLUMNS)
    return (
        f"CREATE TABLE {table} ({definitions}) DUPLICATE KEY(account_id) "
        "DISTRIBUTED BY HASH(account_id) BUCKETS 8 "
        'PROPERTIES ("replication_num" = "1")'
    )


def session_metadata(connection, table: str, dimension: str) -> dict[str, Any]:
    snapshot = connection.metadata()
    for name, query in (
        ("enable_sql_cache", "SHOW VARIABLES LIKE 'enable_sql_cache'"),
        ("enable_query_cache", "SHOW VARIABLES LIKE 'enable_query_cache'"),
        ("exec_mem_limit", "SHOW VARIABLES LIKE 'exec_mem_limit'"),
        (
            "parallel_pipeline_task_num",
            "SHOW VARIABLES LIKE 'parallel_pipeline_task_num'",
        ),
        ("query_timeout", "SHOW VARIABLES LIKE 'query_timeout'"),
        ("fact_ddl", f"SHOW CREATE TABLE {table}"),
        ("dimension_ddl", f"SHOW CREATE TABLE {dimension}"),
    ):
        cursor = connection.execute(query)
        try:
            snapshot[name] = [list(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
    return snapshot


def setup(
    connection,
    table: str,
    dimension: str,
    rows: int,
    dimension_rows: int,
    chunk_rows: int,
    optimized: bool,
    fact_select: Callable[[str, int, int], str],
    dimension_select: Callable[[str, int, int], str],
) -> dict[str, Any]:
    started = time.perf_counter()
    for statement in (dimension_ddl(dimension), ddl(table, optimized)):
        connection.execute(statement).close()
    for name, columns, count, generate in (
        (dimension, DIM_COLUMNS, dimension_rows, dimension_select),
        (table, FACT_COLUMNS, rows, fact_select),
    ):
        for start in range(0, count, chunk_rows):
            end = min(count, start + chunk_rows)
            statement = (
                f"INSERT INTO {name} ({', '.join(columns)}) "
                f"{generate('doris', start, end)}"
            )
            connection.execute(statement).close()
            connection.commit()
    return {
        "profile": "optimized" if optimized else "baseline",
        "engine": "doris",
        "table_model": "DUPLICATE KEY",
        "sort_key": "(event_day, id)" if optimized else "(id)",
        "dimension_sort_key": "(account_id)",
        "built_in_indexes": ["prefix index", "zone map"],
        "data_skipping_index": "id Bloom filter" if optimized else None,
        "replication_num": 1,
        "buckets": 8,
        "load_and_layout_elapsed_ms": (time.perf_counter() - started) * 1000,
        "session": session_metadata(connection, table, dimension),
    }
