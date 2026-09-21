"""Optional remote backend with profile, terminal or macOS Keychain authentication."""

from __future__ import annotations

import gc
import getpass
import sys
import time
import uuid
from pathlib import Path

import pandas as pd

from benchmarks.frame_keychain import keychain_entry
from benchmarks.frame_workloads import ACCOUNTS, fingerprint, sql_query

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
HISTORY_COLUMNS = {
    "total_elapsed_time": "server_total_ms",
    "compilation_time": "server_compilation_ms",
    "execution_time": "server_execution_ms",
    "queued_provisioning_time": "server_provisioning_queue_ms",
    "queued_overload_time": "server_overload_queue_ms",
    "bytes_scanned": "server_bytes_scanned",
}


def snowflake_query(workload: str, fact: str, dimension: str) -> str:
    query = sql_query(workload)
    if workload == "clean_derive":
        query = """
            SELECT id, CAST(FLOOR(amount_cents * quantity *
                (100 - COALESCE(discount_pct, 0)) / 100) AS BIGINT) AS net_cents
            FROM sales
        """
    return query.replace("FROM sales", f"FROM {fact}").replace(
        "JOIN accounts", f"JOIN {dimension}"
    )


def normalize_result(frame: pd.DataFrame | None, columns: list[str]) -> pd.DataFrame:
    if frame is None:
        frame = pd.DataFrame(columns=columns)
    frame.columns = [column.lower() for column in frame.columns]
    # All four workloads return non-null integers. Snowflake may choose int8/32.
    return frame.astype("int64", copy=False)


class SnowflakeBackend:
    """One session owns all temporary stages/tables and closes them together."""

    def __init__(
        self,
        connection_name: str,
        timeout_seconds: int,
        *,
        prompt_password: bool = False,
        use_keychain: bool = False,
    ):
        try:
            import snowflake.connector
        except ImportError as exc:
            raise RuntimeError(
                "Snowflake dependency required: uv sync --extra snowflake"
            ) from exc
        authentication = {}
        password = None
        save_password = False
        if use_keychain:
            keychain, service, user = keychain_entry(connection_name)
            if not prompt_password:
                password = keychain.get_password(service, user)
        if prompt_password or (use_keychain and password is None):
            if not sys.stdin.isatty():
                raise ValueError(
                    "Password entry requires an interactive terminal. "
                    "Run the same command once in your local terminal to register Keychain."
                    if use_keychain
                    else "Password entry requires an interactive terminal"
                )
            destination = "saved to macOS Keychain after login" if use_keychain else "not saved"
            password = getpass.getpass(f"Snowflake password ({destination}): ")
            if not password:
                raise ValueError("Snowflake password must not be empty")
            save_password = use_keychain
        if password is not None:
            authentication = {
                "authenticator": "snowflake",
                "password": password,
                "client_store_temporary_credential": False,
                "client_request_mfa_token": False,
            }
        self.connection = snowflake.connector.connect(
            connection_name=connection_name,
            login_timeout=60,
            network_timeout=timeout_seconds,
            client_session_keep_alive=False,
            session_parameters={
                "USE_CACHED_RESULT": False,
                "QUERY_TAG": f"pandas-duckdb-benchmark-{uuid.uuid4().hex}",
                "STATEMENT_TIMEOUT_IN_SECONDS": timeout_seconds,
                "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": timeout_seconds,
            },
            **authentication,
        )
        self.prefix = f"FRAME_BENCH_{uuid.uuid4().hex.upper()}"
        self.stage = f"{self.prefix}_STAGE"
        self.dimension = f"{self.prefix}_ACCOUNTS"
        self.tables: dict[int, str] = {}
        self.setup_records: list[dict] = []
        self.history_errors: list[str] = []
        try:
            if save_password:
                keychain.set_password(service, user, password)
                print("Snowflake 비밀번호를 macOS 키체인에 저장했습니다.", flush=True)
            with self.connection.cursor() as cursor:
                cursor.execute("""
                    SELECT CURRENT_ACCOUNT(), CURRENT_REGION(), CURRENT_ROLE(),
                           CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA(),
                           CURRENT_VERSION()
                """)
                values = cursor.fetchone()
                self.metadata = dict(
                    zip(
                        [
                            "account",
                            "region",
                            "role",
                            "warehouse",
                            "database",
                            "schema",
                            "version",
                        ],
                        values,
                        strict=True,
                    )
                )
                if not all(
                    self.metadata[key] for key in ("warehouse", "database", "schema")
                ):
                    raise ValueError(
                        "Connection profile must specify warehouse, database and schema"
                    )
                cursor.execute("SHOW WAREHOUSES")
                names = [column[0].lower() for column in cursor.description]
                warehouses = [
                    dict(zip(names, row, strict=True)) for row in cursor.fetchall()
                ]
                warehouse = next(
                    item
                    for item in warehouses
                    if item["name"] == self.metadata["warehouse"]
                )
                self.metadata["warehouse_settings"] = {
                    key: warehouse.get(key)
                    for key in (
                        "size",
                        "type",
                        "auto_suspend",
                        "auto_resume",
                        "min_cluster_count",
                        "max_cluster_count",
                        "scaling_policy",
                        "resource_monitor",
                    )
                }
                self.metadata.update(
                    {
                        "use_cached_result": False,
                        "connector_version": snowflake.connector.__version__,
                        "client_memory_is_not_server_memory": True,
                    }
                )
                cursor.execute(
                    f"CREATE TEMPORARY STAGE {self.stage} FILE_FORMAT=(TYPE=PARQUET)"
                )
        except (Exception, KeyboardInterrupt):
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def load(self, dataset: dict) -> None:
        rows = dataset["rows"]
        if not self.tables:
            self._load_table(
                Path(dataset["dimension"]),
                self.dimension,
                ("account_id", "tier"),
                ACCOUNTS,
                "accounts",
            )
        table = f"{self.prefix}_SALES_{rows}"
        self._load_table(
            Path(dataset["fact"]), table, FACT_COLUMNS, rows, f"sales_{rows}"
        )
        self.tables[rows] = table

    def _load_table(
        self,
        path: Path,
        table: str,
        columns: tuple,
        expected_rows: int,
        stage_folder: str,
    ) -> None:
        location = f"@{self.stage}/{stage_folder}"
        # Path is a generated local fixture. Quote its URI as a SQL string literal.
        uri = "file://" + str(path.resolve())
        quoted_uri = "'" + uri.replace("'", "''") + "'"
        definitions = ", ".join(f"{column} NUMBER(18,0)" for column in columns)
        projection = ", ".join(f"$1:{column}::NUMBER(18,0)" for column in columns)
        with self.connection.cursor() as cursor:
            cursor.execute(f"CREATE TEMPORARY TABLE {table} ({definitions})")
            start = time.perf_counter()
            cursor.execute(
                f"PUT {quoted_uri} {location} AUTO_COMPRESS=FALSE OVERWRITE=FALSE"
            )
            put_result = cursor.fetchall()
            upload_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            cursor.execute(
                f"COPY INTO {table} FROM (SELECT {projection} FROM {location}) "
                "ON_ERROR=ABORT_STATEMENT"
            )
            copy_id = cursor.sfqid
            copy_result = cursor.fetchall()
            load_ms = (time.perf_counter() - start) * 1000
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            actual_rows = int(cursor.fetchone()[0])
            if actual_rows != expected_rows:
                raise ValueError(
                    f"Snowflake row count mismatch: {actual_rows} != {expected_rows}"
                )
        self.setup_records.append(
            {
                "file": str(path),
                "table": table,
                "rows": actual_rows,
                "upload_ms": upload_ms,
                "copy_ms": load_ms,
                "copy_query_id": copy_id,
                "put_result": put_result,
                "copy_result": copy_result,
            }
        )

    def _execute(self, query: str) -> tuple[pd.DataFrame, dict]:
        with self.connection.cursor() as cursor:
            cpu_start = time.process_time_ns()
            start = time.perf_counter_ns()
            cursor.execute(query)
            executed = time.perf_counter_ns()
            output = cursor.fetch_pandas_all()
            output = normalize_result(
                output, [column[0] for column in cursor.description]
            )
            finished = time.perf_counter_ns()
            sample = {
                "elapsed_ms": (finished - start) / 1e6,
                "execute_roundtrip_ms": (executed - start) / 1e6,
                "fetch_dataframe_ms": (finished - executed) / 1e6,
                "cpu_ms": (time.process_time_ns() - cpu_start) / 1e6,
                "query_id": cursor.sfqid,
                "output_rows": len(output),
            }
        return output, sample

    def run(self, case: dict, runs: int, profile: bool) -> dict:
        query = snowflake_query(
            case["workload"], self.tables[case["rows"]], self.dimension
        )
        output, _ = self._execute(query)
        expected = fingerprint(output)
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
                raise ValueError("Snowflake repeated result does not match warmup")
            sample["validated"] = True
            measurements.append(sample)
            del output
        self._add_query_history(measurements)
        return {"fingerprint": expected, "measurements": measurements}

    def _add_query_history(self, measurements: list[dict]) -> None:
        for sample in measurements:
            sample.update(dict.fromkeys(HISTORY_COLUMNS.values()))
        placeholders = ",".join(["%s"] * len(measurements))
        query = (
            "SELECT QUERY_ID, "
            + ", ".join(HISTORY_COLUMNS)
            + " FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT=>1000))"
            + f" WHERE QUERY_ID IN ({placeholders})"
        )
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    query, tuple(sample["query_id"] for sample in measurements)
                )
                history = {row[0]: row[1:] for row in cursor.fetchall()}
        except Exception as exc:
            # Wall-clock measurements remain useful if history access is restricted.
            self.history_errors.append(f"{type(exc).__name__}: {exc}")
            return
        for sample in measurements:
            if sample["query_id"] in history:
                sample.update(
                    dict(
                        zip(
                            HISTORY_COLUMNS.values(),
                            history[sample["query_id"]],
                            strict=True,
                        )
                    )
                )
