"""Small MySQL/Stream Load client shared by the two Doris-only experiments."""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid Doris identifier: {value!r}")
    return f"`{value}`"


class DorisClient:
    def __init__(self, connection, database: str):
        self.connection = connection
        self.database = database

    def execute(self, sql: str):
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
        except BaseException:
            cursor.close()
            raise
        return cursor

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def backends(self) -> list[dict]:
        with self.execute("SHOW BACKENDS") as cursor:
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def wait_ready(self, attempts: int = 60) -> None:
        for _ in range(attempts):
            if any(
                str(backend["Alive"]).lower() in ("true", "1")
                and float(str(backend["AvailCapacity"]).split()[0]) > 0
                for backend in self.backends()
            ):
                return
            time.sleep(1)
        raise TimeoutError("Doris backend did not report available storage in time")

    def metadata(self) -> dict:
        with self.execute("SELECT VERSION()") as cursor:
            version = cursor.fetchone()[0]
        backends = self.backends()
        return {
            "version": backends[0].get("Version") if backends else None,
            "mysql_compatibility_version": version,
            "database": self.database,
            "sql_host": os.environ.get("DORIS_HOST", "127.0.0.1"),
            "sql_port": int(os.environ.get("DORIS_SQL_PORT", "19030")),
            "sql_cache": False,
            "query_cache": False,
            "backends": backends,
        }

    def stream_load(
        self,
        table: str,
        path: Path,
        format: str = "parquet",
        columns: str | None = None,
    ) -> dict:
        import requests

        identifier(table)
        host = os.environ.get(
            "DORIS_HTTP_HOST", os.environ.get("DORIS_HOST", "127.0.0.1")
        )
        port = int(os.environ.get("DORIS_HTTP_PORT", "18040"))
        url = f"http://{host}:{port}/api/{self.database}/{table}/_stream_load"
        headers = {
            "Expect": "100-continue",
            "format": format,
            "label": f"benchmark_{uuid.uuid4().hex}",
            "strict_mode": "true",
            "max_filter_ratio": "0",
        }
        if columns:
            headers["columns"] = columns
        started = time.perf_counter()
        with Path(path).open("rb") as handle:
            response = requests.put(
                url,
                data=handle,
                headers=headers,
                auth=(
                    os.environ.get("DORIS_USER", "root"),
                    os.environ.get("DORIS_PASSWORD", ""),
                ),
                timeout=int(os.environ.get("DORIS_LOAD_TIMEOUT", "1800")),
                allow_redirects=False,
            )
        response.raise_for_status()
        result = response.json()
        if result.get("Status") != "Success" or int(
            result.get("NumberFilteredRows", 0)
        ):
            raise RuntimeError(f"Doris Stream Load failed: {result}")
        return {**result, "client_elapsed_ms": (time.perf_counter() - started) * 1000}


def connect(database: str, create: bool = True) -> DorisClient:
    import pymysql

    identifier(database)
    last_error = None
    for _ in range(60):
        try:
            connection = pymysql.connect(
                host=os.environ.get("DORIS_HOST", "127.0.0.1"),
                port=int(os.environ.get("DORIS_SQL_PORT", "19030")),
                user=os.environ.get("DORIS_USER", "root"),
                password=os.environ.get("DORIS_PASSWORD", ""),
                autocommit=True,
                connect_timeout=5,
                read_timeout=int(os.environ.get("DORIS_QUERY_TIMEOUT", "1800")),
                write_timeout=60,
            )
            break
        except pymysql.OperationalError as error:
            last_error = error
            if error.args[0] not in (2002, 2003, 2013):
                raise
            time.sleep(1)
    else:
        raise last_error
    client = DorisClient(connection, database)
    try:
        client.wait_ready()
        if create:
            client.execute(
                f"CREATE DATABASE IF NOT EXISTS {identifier(database)}"
            ).close()
        client.execute(f"USE {identifier(database)}").close()
        for setting in ("enable_sql_cache", "enable_query_cache"):
            client.execute(f"SET {setting}=false").close()
    except BaseException:
        client.close()
        raise
    return client
