"""Four dedicated warehouse sizes, shared tables and platform query timing."""

import time

from benchmarks.frame_snowflake import snowflake_query
from benchmarks.sweep_workloads import OUTPUTS, checksum_sql

WAREHOUSES = {
    "snowflake_xsmall": ("FRAME_SWEEP_XSMALL", "X-Small"),
    "snowflake_small": ("FRAME_SWEEP_SMALL", "Small"),
    "snowflake_medium": ("FRAME_SWEEP_MEDIUM", "Medium"),
    "snowflake_large": ("FRAME_SWEEP_LARGE", "Large"),
}


def configure(backend):
    with backend.connection.cursor() as cursor:
        for name, size in WAREHOUSES.values():
            cursor.execute(
                f"CREATE WAREHOUSE IF NOT EXISTS {name} WAREHOUSE_TYPE='STANDARD' WAREHOUSE_SIZE='{size}' MIN_CLUSTER_COUNT=1 MAX_CLUSTER_COUNT=1 AUTO_SUSPEND=60 AUTO_RESUME=TRUE INITIALLY_SUSPENDED=TRUE COMMENT='Six-path pandas DuckDB Snowflake benchmark'"
            )
        cursor.execute("SHOW WAREHOUSES")
        keys = [c[0].lower() for c in cursor.description]
        records = {
            r["name"]: r
            for r in (
                dict(zip(keys, values, strict=True)) for values in cursor.fetchall()
            )
        }
    selected = {}
    for path, (name, size) in WAREHOUSES.items():
        record = records[name]
        assert record["size"] == size and record["type"] == "STANDARD"
        assert record["min_cluster_count"] == record["max_cluster_count"] == 1
        assert record["auto_suspend"] == 60
        selected[path] = {
            k: record.get(k)
            for k in (
                "name",
                "size",
                "type",
                "generation",
                "min_cluster_count",
                "max_cluster_count",
                "auto_suspend",
                "auto_resume",
            )
        }
    backend.connection.client_prefetch_threads = 1
    return selected


def use(backend, path):
    with backend.connection.cursor() as cursor:
        cursor.execute(f"USE WAREHOUSE {WAREHOUSES[path][0]}")


def suspend(backend, path):
    name = WAREHOUSES[path][0]
    with backend.connection.cursor() as cursor:
        cursor.execute(f"SHOW WAREHOUSES LIKE '{name}'")
        keys = [c[0].lower() for c in cursor.description]
        record = dict(zip(keys, cursor.fetchone(), strict=True))
        if record["state"] == "STARTED":
            cursor.execute(f"ALTER WAREHOUSE {name} SUSPEND")


def block(backend, path, rows, work, runs, expected):
    query = snowflake_query(work, backend.tables[rows], backend.dimension)
    with backend.connection.cursor() as cursor:
        cursor.execute(checksum_sql(query, work))
        values = cursor.fetchone()
        checked = dict(
            columns=OUTPUTS[work],
            rows=int(values[0]),
            sum_h=int(values[1]),
            sum_h2=int(values[2]),
        )
        if checked != expected:
            raise ValueError(f"Full-result SQL checksum mismatch: {path}/{rows}/{work}")
        checksum_query_id = cursor.sfqid
        # Warm the actual query too: checksum aggregation has a different plan.
        cursor.execute(query)
        if cursor.rowcount != expected["rows"]:
            raise ValueError("Snowflake warmup output row count mismatch")
        warmup_query_id = cursor.sfqid
        measurements = []
        for _ in range(runs):
            started = time.perf_counter_ns()
            cursor.execute(query)
            roundtrip = (time.perf_counter_ns() - started) / 1e6
            if cursor.rowcount != expected["rows"]:
                raise ValueError("Snowflake measured output row count mismatch")
            measurements.append(
                {
                    "query_id": cursor.sfqid,
                    "client_execute_roundtrip_ms": roundtrip,
                    "output_rows": cursor.rowcount,
                    "validation": "row-count; full checksum query before block",
                }
            )
    backend._add_query_history(measurements)
    for sample in measurements:
        if sample["server_total_ms"] is None:
            # Query history can briefly lag query completion.
            for _ in range(3):
                time.sleep(1)
                backend._add_query_history([sample])
                if sample["server_total_ms"] is not None:
                    break
        if sample["server_total_ms"] is None:
            raise ValueError(
                "Platform timing is unavailable; no client-time substitution"
            )
        sample["elapsed_ms"] = sample["server_total_ms"]
    return {
        "checksum": checked,
        "checksum_query_id": checksum_query_id,
        "warmup_query_id": warmup_query_id,
        "measurements": measurements,
    }
