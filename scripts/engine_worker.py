"""Create data, validate results, and measure one database engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "v3"
TABLE = "benchmark"
DIM_TABLE = "account_dim"
DIM_ROWS = 1_000_000
CHUNK_ROWS = 1_000_000
SQLITE_BATCH_ROWS = 100_000
RESULTS = Path(os.environ.get("BENCH_RESULTS", "/results"))
DATA = Path(os.environ.get("BENCH_DATA", "/data"))
PROFILE = os.environ.get("BENCH_PROFILE", "baseline")


@dataclass(frozen=True)
class QuerySpec:
    name: str
    sql: str


def rounded(engine: str, expression: str, digits: int) -> str:
    if engine == "postgres":
        if expression.startswith("SUM(") and expression.endswith(")"):
            inner = expression[4:-1]
            expression = f"SUM(CAST({inner} AS NUMERIC))"
        return f"ROUND(CAST({expression} AS NUMERIC), {digits})"
    return f"ROUND({expression}, {digits})"


def amount_sum(engine: str, column: str) -> str:
    if engine == "clickhouse":
        cents = f"toInt64(round({column} * 100))"
    elif engine == "postgres":
        cents = f"CAST(ROUND(CAST({column} AS NUMERIC) * 100) AS BIGINT)"
    else:
        cents = f"CAST(ROUND({column} * 100) AS BIGINT)"
    return f"SUM({cents}) / 100.0"


def average_discount(engine: str, column: str) -> str:
    if engine == "clickhouse":
        cents = f"toInt64(round({column} * 100))"
        expression = f"SUM({cents}) / COUNT() / 100.0"
    elif engine == "postgres":
        cents = f"CAST(ROUND(CAST({column} AS NUMERIC) * 100) AS BIGINT)"
        expression = f"SUM({cents})::NUMERIC / COUNT(*) / 100.0"
    else:
        cents = f"CAST(ROUND({column} * 100) AS BIGINT)"
        expression = f"SUM({cents}) * 1.0 / COUNT(*) / 100.0"
    return rounded(engine, expression, 6)


def query_specs(rows: int, engine: str) -> list[QuerySpec]:
    target = max(0, min(rows - 1, 987_654_321))
    return [
        QuerySpec(
            "small",
            f"SELECT id, account_id, category_id, amount FROM {TABLE} "
            f"WHERE id = {target};",
        ),
        QuerySpec(
            "medium",
            f"SELECT region_id, COUNT(*) AS row_count, "
            f"{amount_sum(engine, 'amount')} AS amount_sum, "
            f"{average_discount(engine, 'discount')} AS avg_discount FROM {TABLE} "
            "WHERE event_day BETWEEN 600 AND 699 "
            "GROUP BY region_id ORDER BY region_id;",
        ),
        QuerySpec(
            "large",
            f"SELECT category_id, COUNT(*) AS row_count, "
            f"{amount_sum(engine, 'amount')} AS amount_sum, "
            f"SUM(quantity) AS quantity_sum "
            f"FROM {TABLE} GROUP BY category_id ORDER BY category_id;",
        ),
        QuerySpec(
            "join",
            f"SELECT b.region_id, d.account_tier, COUNT(*) AS row_count, "
            f"{amount_sum(engine, 'b.amount')} AS amount_sum "
            f"FROM {TABLE} b JOIN {DIM_TABLE} d "
            "ON b.account_id = d.account_id "
            "GROUP BY b.region_id, d.account_tier "
            "ORDER BY b.region_id, d.account_tier;",
        ),
    ]


def row_values(i: int) -> tuple[Any, ...]:
    return (
        i,
        i % 1_000_000,
        i % 1_000,
        i % 50,
        (i % 100) + 1,
        (i % 100_000) / 100.0,
        (i % 20) / 100.0,
        i % 1_825,
        i % 86_400,
        i % 2,
        i % 5,
        i % 20,
        i % 1_000,
        (i // 10) % 1_000,
        (i // 100) % 1_000,
        (i // 1_000) % 1_000,
        (i // 10_000) % 1_000,
        (i // 100_000) % 1_000,
        (i // 1_000_000) % 1_000,
        (i // 10_000_000) % 1_000,
    )


def dimension_row_values(i: int) -> tuple[int, int, int]:
    return (i, (i // 1_000) % 5, i % 50)


def connect(engine: str):
    if engine == "postgres":
        import psycopg

        last_error = None
        for _ in range(30):
            try:
                return psycopg.connect(
                    host="postgres",
                    port=5432,
                    user="benchmark",
                    password="benchmark",
                    dbname="benchmark",
                )
            except psycopg.OperationalError as error:
                last_error = error
                time.sleep(1)
        raise last_error
    if engine == "clickhouse":
        import clickhouse_connect
        from clickhouse_connect.driver.exceptions import Error as ClickHouseError

        last_error = None
        for _ in range(30):
            try:
                return clickhouse_connect.get_client(
                    host="clickhouse",
                    port=8123,
                    username="benchmark",
                    password="benchmark",
                )
            except ClickHouseError as error:
                last_error = error
                time.sleep(1)
        raise last_error
    if engine == "duckdb":
        import duckdb

        DATA.joinpath("duckdb").mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(DATA / "duckdb" / "benchmark.duckdb"))
    if engine == "sqlite":
        sqlite_dir = DATA / "sqlite"
        sqlite_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("SQLITE_TMPDIR", str(sqlite_dir / "tmp"))
        (sqlite_dir / "tmp").mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(sqlite_dir / "benchmark.sqlite"))
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA temp_store = FILE")
        return connection
    raise ValueError(f"unknown engine: {engine}")


def close(connection) -> None:
    connection.close()


def execute(connection, engine: str, sql: str) -> list[tuple[Any, ...]]:
    if engine == "clickhouse":
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            return [tuple(row) for row in connection.query(sql).result_rows]
        connection.command(sql)
        return []
    cursor = connection.execute(sql)
    if cursor.description is None:
        return []
    return [tuple(row) for row in cursor.fetchall()]


def commit(connection, engine: str) -> None:
    if engine != "clickhouse":
        connection.commit()


def count_rows(connection, engine: str, table: str) -> int:
    result = execute(connection, engine, f"SELECT COUNT(*) FROM {table}")
    return int(result[0][0])


def ddl(engine: str, optimized: bool = False) -> str:
    if engine == "clickhouse":
        order_by = "(event_day, id)" if optimized else "tuple()"
        index_clause = (
            ", INDEX id_bloom_idx id TYPE bloom_filter(0.01) GRANULARITY 4"
            if optimized
            else ""
        )
        return f"""
        CREATE TABLE {TABLE} (
          id UInt64, account_id UInt64, category_id UInt32, region_id UInt32,
          quantity UInt32, amount Float64, discount Float64,
          event_day UInt32, event_second UInt32, is_active UInt8,
          status_id UInt8, source_id UInt8,
          metric_01 UInt64, metric_02 UInt64, metric_03 UInt64, metric_04 UInt64,
          metric_05 UInt64, metric_06 UInt64, metric_07 UInt64, metric_08 UInt64
          {index_clause}
        ) ENGINE = MergeTree ORDER BY {order_by}
        """
    numeric = "BIGINT" if engine == "postgres" else "INTEGER"
    real = "DOUBLE PRECISION" if engine == "postgres" else "REAL"
    return f"""
    CREATE TABLE {TABLE} (
      id {numeric}, account_id {numeric}, category_id {numeric}, region_id {numeric},
      quantity {numeric}, amount {real}, discount {real}, event_day {numeric},
      event_second {numeric}, is_active {numeric}, status_id {numeric}, source_id {numeric},
      metric_01 {numeric}, metric_02 {numeric}, metric_03 {numeric}, metric_04 {numeric},
      metric_05 {numeric}, metric_06 {numeric}, metric_07 {numeric}, metric_08 {numeric}
    )
    """


def dimension_ddl(engine: str, optimized: bool = False) -> str:
    if engine == "clickhouse":
        return f"""
        CREATE TABLE {DIM_TABLE} (
          account_id UInt64, account_tier UInt32, account_region_id UInt32
        ) ENGINE = MergeTree ORDER BY account_id
        """
    numeric = "BIGINT" if engine == "postgres" else "INTEGER"
    return f"""
    CREATE TABLE {DIM_TABLE} (
      account_id {numeric}, account_tier {numeric}, account_region_id {numeric}
    )
    """


def optimized_index_statements(engine: str) -> list[tuple[str, str]]:
    if engine in ("clickhouse", "duckdb"):
        return []
    return [
        ("benchmark_id_idx", f"CREATE INDEX benchmark_id_idx ON {TABLE}(id)"),
        (
            "benchmark_event_day_idx",
            f"CREATE INDEX benchmark_event_day_idx ON {TABLE}(event_day)",
        ),
        (
            "benchmark_account_id_idx",
            f"CREATE INDEX benchmark_account_id_idx ON {TABLE}(account_id)",
        ),
        (
            "account_dim_account_id_idx",
            f"CREATE INDEX account_dim_account_id_idx ON {DIM_TABLE}(account_id)",
        ),
    ]


def apply_optimized_indexes(connection, engine: str) -> dict[str, Any]:
    started = time.perf_counter()
    built = []
    for name, sql in optimized_index_statements(engine):
        index_started = time.perf_counter()
        execute(connection, engine, sql)
        commit(connection, engine)
        built.append(
            {
                "name": name,
                "elapsed_ms": (time.perf_counter() - index_started) * 1000,
            }
        )
    return {
        "profile": PROFILE,
        "engine": engine,
        "sort_key": "(event_day, id)" if engine == "clickhouse" else None,
        "data_skipping_index": "id bloom_filter" if engine == "clickhouse" else None,
        "indexes": built,
        "total_elapsed_ms": (time.perf_counter() - started) * 1000,
    }


def generated_select(
    engine: str,
    start: int,
    end: int,
    order_by: str | None = None,
    source_override: str | None = None,
    id_expression: str | None = None,
) -> str:
    if engine == "clickhouse":
        source = f"numbers({start}, {end - start})"
        i = "number"

        def integer_division(value: str, divisor: int) -> str:
            return f"intDiv({value}, {divisor})"
    elif engine == "postgres":
        source = f"generate_series({start}, {end - 1}) AS generated(i)"
        i = "i"

        def integer_division(value: str, divisor: int) -> str:
            return f"({value} / {divisor})"
    else:
        source = f"range({start}, {end}) AS generated(i)"
        i = "i"

        def integer_division(value: str, divisor: int) -> str:
            operator = "//" if engine == "duckdb" else "/"
            return f"({value} {operator} {divisor})"

    if source_override is not None:
        source = source_override
    if id_expression is not None:
        i = f"({id_expression})"

    sql = f"""
    SELECT {i} AS id, {i} % 1000000 AS account_id,
      {i} % 1000 AS category_id, {i} % 50 AS region_id,
      ({i} % 100) + 1 AS quantity, ({i} % 100000) / 100.0 AS amount,
      ({i} % 20) / 100.0 AS discount, {i} % 1825 AS event_day,
      {i} % 86400 AS event_second, {i} % 2 AS is_active,
      {i} % 5 AS status_id, {i} % 20 AS source_id,
      {i} % 1000 AS metric_01, {integer_division(i, 10)} % 1000 AS metric_02,
      {integer_division(i, 100)} % 1000 AS metric_03,
      {integer_division(i, 1000)} % 1000 AS metric_04,
      {integer_division(i, 10000)} % 1000 AS metric_05,
      {integer_division(i, 100000)} % 1000 AS metric_06,
      {integer_division(i, 1000000)} % 1000 AS metric_07,
      {integer_division(i, 10000000)} % 1000 AS metric_08
    FROM {source}
    """
    if order_by:
        sql += f"\n    ORDER BY {order_by}\n"
    return sql


def generated_dimension_select(engine: str, start: int, end: int) -> str:
    if engine == "clickhouse":
        source = f"numbers({start}, {end - start})"
        i = "number"
        tier = f"intDiv({i}, 1000) % 5"
    elif engine == "postgres":
        source = f"generate_series({start}, {end - 1}) AS generated(i)"
        i = "i"
        tier = f"({i} / 1000) % 5"
    else:
        source = f"range({start}, {end}) AS generated(i)"
        i = "i"
        operator = "//" if engine == "duckdb" else "/"
        tier = f"({i} {operator} 1000) % 5"
    return f"""
    SELECT {i} AS account_id, {tier} AS account_tier,
      {i} % 50 AS account_region_id
    FROM {source}
    """


def setup(engine: str, rows: int, force_reload: bool) -> None:
    optimized = PROFILE == "optimized"
    result_dir = RESULTS / engine
    result_dir.mkdir(parents=True, exist_ok=True)
    marker_path = result_dir / "dataset.json"
    if marker_path.exists() and not force_reload:
        marker = json.loads(marker_path.read_text())
        if (
            marker.get("rows") == rows
            and marker.get("schema_version") == SCHEMA_VERSION
        ):
            print(f"{engine}: 기존 데이터 사용", flush=True)
            return
        raise RuntimeError(
            f"{engine}: 기존 데이터 크기가 다릅니다. --force-reload를 사용하세요."
        )

    connection = connect(engine)
    optimization = None
    try:
        if force_reload:
            execute(connection, engine, f"DROP TABLE IF EXISTS {DIM_TABLE}")
            execute(connection, engine, f"DROP TABLE IF EXISTS {TABLE}")
            commit(connection, engine)
        if engine == "duckdb":
            if optimized:
                execute(connection, engine, "SET preserve_insertion_order = true")
                execute(connection, engine, "SET threads = 1")
            execute(
                connection,
                engine,
                f"CREATE TABLE {DIM_TABLE} AS "
                f"{generated_dimension_select(engine, 0, DIM_ROWS)}",
            )
            execute(
                connection,
                engine,
                f"CREATE TABLE {TABLE} AS "
                f"{generated_select(engine, 0, 0)}",
            )
            if optimized:
                layout_started = time.perf_counter()
                base, remainder = divmod(rows, 1_825)
                for day in range(1_825):
                    day_rows = base + (1 if day < remainder else 0)
                    if day_rows == 0:
                        continue
                    day_source = (
                        f"range(0, {day_rows}) AS generated(k)"
                    )
                    day_id = f"{day} + 1825 * k"
                    execute(
                        connection,
                        engine,
                        f"INSERT INTO {TABLE} "
                        f"{generated_select('duckdb', 0, day_rows, source_override=day_source, id_expression=day_id)}",
                    )
                    commit(connection, engine)
                optimization = {
                    "profile": PROFILE,
                    "engine": engine,
                    "sort_key": "(event_day, id)",
                    "data_skipping_index": "DuckDB zone maps (ordered load)",
                    "indexes": [],
                    "sort_elapsed_ms": (time.perf_counter() - layout_started) * 1000,
                }
        elif engine == "sqlite":
            connection.execute(dimension_ddl(engine, optimized))
            connection.execute(ddl(engine, optimized))
            dimension_insert_sql = (
                f"INSERT INTO {DIM_TABLE} VALUES ({','.join('?' for _ in range(3))})"
            )
            insert_sql = (
                f"INSERT INTO {TABLE} VALUES ({','.join('?' for _ in range(20))})"
            )
            for start in range(0, DIM_ROWS, SQLITE_BATCH_ROWS):
                end = min(DIM_ROWS, start + SQLITE_BATCH_ROWS)
                connection.executemany(
                    dimension_insert_sql,
                    (dimension_row_values(i) for i in range(start, end)),
                )
                connection.commit()
            for start in range(0, rows, SQLITE_BATCH_ROWS):
                end = min(rows, start + SQLITE_BATCH_ROWS)
                connection.executemany(
                    insert_sql, (row_values(i) for i in range(start, end))
                )
                connection.commit()
            if optimized:
                optimization = apply_optimized_indexes(connection, engine)
        else:
            execute(connection, engine, dimension_ddl(engine, optimized))
            execute(connection, engine, ddl(engine, optimized))
            for start in range(0, DIM_ROWS, CHUNK_ROWS):
                end = min(DIM_ROWS, start + CHUNK_ROWS)
                execute(
                    connection,
                    engine,
                    f"INSERT INTO {DIM_TABLE} "
                    f"{generated_dimension_select(engine, start, end)}",
                )
                commit(connection, engine)
            for start in range(0, rows, CHUNK_ROWS):
                end = min(rows, start + CHUNK_ROWS)
                execute(
                    connection,
                    engine,
                    f"INSERT INTO {TABLE} {generated_select(engine, start, end)}",
                )
                commit(connection, engine)
            if optimized:
                optimization = apply_optimized_indexes(connection, engine)
    finally:
        close(connection)

    if optimized:
        (result_dir / "optimization.json").write_text(
            json.dumps(optimization, ensure_ascii=False, indent=2)
        )

    connection = connect(engine)
    try:
        actual_rows = count_rows(connection, engine, TABLE)
        actual_dimension_rows = count_rows(connection, engine, DIM_TABLE)
    finally:
        close(connection)
    if actual_rows != rows:
        raise RuntimeError(f"{engine}: 행 수 불일치: {actual_rows:,} != {rows:,}")
    if actual_dimension_rows != DIM_ROWS:
        raise RuntimeError(
            f"{engine}: dimension 행 수 불일치: "
            f"{actual_dimension_rows:,} != {DIM_ROWS:,}"
        )

    marker_path.write_text(
        json.dumps(
            {
                "rows": rows,
                "actual_rows": actual_rows,
                "dimension_rows": DIM_ROWS,
                "actual_dimension_rows": actual_dimension_rows,
                "schema_version": SCHEMA_VERSION,
            },
            indent=2,
        ),
    )
    print(f"{engine}: {rows:,}행 적재 완료", flush=True)


def normalize(value: Any) -> Any:
    if isinstance(value, (Decimal, float)):
        value = float(value)
        if math.isnan(value):
            return None
        value = round(value, 9)
        return int(value) if value.is_integer() else value
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


def digest_rows(rows: Iterable[tuple[Any, ...]]) -> str:
    normalized = [[normalize(value) for value in row] for row in rows]
    payload = json.dumps(
        normalized, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def validation_path(engine: str) -> Path:
    return RESULTS / engine / "validation.json"


def validate(engine: str, rows: int) -> None:
    connection = connect(engine)
    validation: dict[str, Any] = {"engine": engine, "rows": rows, "queries": {}}
    try:
        for spec in query_specs(rows, engine):
            started = time.perf_counter()
            result = execute(connection, engine, spec.sql)
            elapsed_ms = (time.perf_counter() - started) * 1000
            validation["queries"][spec.name] = {
                "row_count": len(result),
                "digest": digest_rows(result),
                "rows": [[normalize(value) for value in row] for row in result],
                "validation_elapsed_ms": elapsed_ms,
            }
    finally:
        close(connection)
    validation_path(engine).write_text(
        json.dumps(validation, ensure_ascii=False, indent=2)
    )
    print(f"{engine}: 결과 검증 완료", flush=True)


def measure(engine: str, rows: int, runs: int) -> None:
    validation = json.loads(validation_path(engine).read_text())
    connection = connect(engine)
    raw_path = RESULTS / engine / "measurements.jsonl"
    summaries: dict[str, Any] = {}
    try:
        with raw_path.open("w") as raw_file:
            for spec in query_specs(rows, engine):
                expected = validation["queries"][spec.name]
                samples: list[float] = []
                for iteration in range(1, runs + 1):
                    started = time.perf_counter()
                    result = execute(connection, engine, spec.sql)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    actual_digest = digest_rows(result)
                    if actual_digest != expected["digest"]:
                        raise RuntimeError(
                            f"{engine}/{spec.name}: 결과 checksum 불일치: "
                            f"{actual_digest} != {expected['digest']}"
                        )
                    samples.append(elapsed_ms)
                    raw_file.write(
                        json.dumps(
                            {
                                "engine": engine,
                                "query": spec.name,
                                "iteration": iteration,
                                "elapsed_ms": elapsed_ms,
                                "row_count": len(result),
                                "digest": actual_digest,
                            }
                        )
                        + "\n"
                    )
                summaries[spec.name] = {
                    "runs": runs,
                    "average_ms": statistics.fmean(samples),
                    "min_ms": min(samples),
                    "max_ms": max(samples),
                    "stddev_ms": statistics.stdev(samples) if runs > 1 else 0.0,
                    "p50_ms": statistics.median(samples),
                    "p95_ms": sorted(samples)[max(0, math.ceil(runs * 0.95) - 1)],
                    "row_count": expected["row_count"],
                    "digest": expected["digest"],
                }
    finally:
        close(connection)
    (RESULTS / engine / "summary.json").write_text(
        json.dumps({"engine": engine, "rows": rows, "queries": summaries}, indent=2)
    )
    print(f"{engine}: {runs}회 측정 완료", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--engine",
        required=True,
        choices=("clickhouse", "duckdb", "sqlite", "postgres"),
    )
    parser.add_argument(
        "--action", required=True, choices=("setup", "validate", "measure")
    )
    parser.add_argument("--rows", required=True, type=int)
    parser.add_argument("--runs", required=True, type=int)
    parser.add_argument("--force-reload", action="store_true")
    parser.add_argument(
        "--profile", choices=("baseline", "optimized"), default="baseline"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    global PROFILE
    PROFILE = args.profile
    if args.action == "setup":
        setup(args.engine, args.rows, args.force_reload)
    elif args.action == "validate":
        validate(args.engine, args.rows)
    else:
        measure(args.engine, args.rows, args.runs)


if __name__ == "__main__":
    main()
