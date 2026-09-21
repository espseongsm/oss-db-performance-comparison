"""Cortex Search with existing BGE vectors and task-scoped cloud resources."""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import numpy as np


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def vector_query(vector, doc=None):
    request = {
        "multi_index_query": {"EMBEDDING": [{"vector": vector.tolist()}]},
        "columns": ["ID"],
        "limit": 10,
        "scoring_config": {
            "reranker": "none",
            "weights": {"texts": 0, "vectors": 1, "reranker": 0},
        },
    }
    if doc is not None:
        request["filter"] = {"@eq": {"DOC_NAME": doc}}
    return request


class CortexBackend:
    def __init__(self, vectors, chunks, directory):
        import pyarrow as pa
        import pyarrow.parquet as pq
        import snowflake.connector
        from snowflake.connector.config_manager import CONFIG_MANAGER
        from snowflake.core import Root

        from benchmarks.frame_keychain import keychain_entry

        self.directory = directory
        self.schema_created = False
        self.database_created = False
        self.connection = None
        self.events = []
        CONFIG_MANAGER.read_config()
        profile_name = os.environ.get("FINANCEBENCH_SNOWFLAKE_CONNECTION", "benchmark")
        profile = CONFIG_MANAGER["connections"][profile_name]
        auth = {}
        if profile.get("authenticator", "snowflake").lower() == "snowflake":
            keyring, service, user = keychain_entry(profile_name)
            password = keyring.get_password(service, user)
            if password:
                auth["password"] = password
        self.schema = "FINANCEBENCH_" + uuid.uuid4().hex[:12].upper()
        try:
            self.connection = snowflake.connector.connect(
                connection_name=profile_name,
                **auth,
                login_timeout=60,
                network_timeout=1800,
                client_session_keep_alive=False,
                session_parameters={
                    "QUERY_TAG": self.schema,
                    "USE_CACHED_RESULT": False,
                    "STATEMENT_TIMEOUT_IN_SECONDS": 1800,
                    "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": 120,
                },
            )
            cursor = self.connection.cursor()
            version, region, database, warehouse = cursor.execute(
                "SELECT CURRENT_VERSION(), CURRENT_REGION(), CURRENT_DATABASE(), CURRENT_WAREHOUSE()"
            ).fetchone()
            if not database or not warehouse:
                raise ValueError("Snowflake profile requires a database and warehouse")
            self.version = version
            if database.startswith("USER$"):
                # Personal databases reject regular tables. Isolate this run in a
                # uniquely named database instead; remove it on success or failure.
                database = self.schema + "_DB"
                self.owned_database = identifier(database)
                self.execute(f"CREATE DATABASE {self.owned_database}")
                self.database_created = True
            self.qualified_schema = f"{identifier(database)}.{identifier(self.schema)}"
            self.plan = {
                "region": region,
                "transport": "snowflake-core Python API (HTTPS from benchmark Mac)",
                "index": "Cortex managed ANN; supplied 384-dimensional BGE vectors",
                "query": vector_query(vectors[0]),
                "resource_schema": self.schema,
                "database": database,
                "warehouse": warehouse,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "embedding_generation": False,
                "index_parameters": "Managed by Snowflake, not matched to OSS HNSW parameters",
            }
            cursor.execute("SHOW WAREHOUSES LIKE %s", (warehouse,))
            names = [d[0] for d in cursor.description]
            wh = dict(zip(names, cursor.fetchone()))
            self.plan["warehouse_configuration"] = {
                k: wh.get(k) for k in ["size", "type", "auto_suspend", "auto_resume"]
            }
            (directory / "cloud-plan.json").write_text(json.dumps(self.plan, indent=2))
            self.execute(
                f"CREATE SCHEMA {self.qualified_schema} DATA_RETENTION_TIME_IN_DAYS=1"
            )
            self.schema_created = True
            self.execute(f"CREATE TEMPORARY STAGE {self.qualified_schema}.UPLOAD")
            path = (directory / "upload.parquet").resolve()
            table = pa.table(
                {
                    "id": pa.array(range(len(chunks)), type=pa.int64()),
                    "doc_name": [c["doc_name"] for c in chunks],
                    "page": pa.array([c["page"] for c in chunks], type=pa.int32()),
                    "embedding": pa.FixedSizeListArray.from_arrays(
                        pa.array(vectors.ravel()), vectors.shape[1]
                    ),
                }
            )
            pq.write_table(table, path, compression="zstd")
            print(f"cortex: uploading {len(chunks):,} existing vectors", flush=True)
            self.execute(
                f"PUT 'file://{str(path).replace(chr(39), chr(39) * 2)}' @{self.qualified_schema}.UPLOAD AUTO_COMPRESS=FALSE PARALLEL=4"
            )
            self.execute(
                f"CREATE TABLE {self.qualified_schema}.RAW (ID NUMBER, DOC_NAME VARCHAR, PAGE NUMBER, EMBEDDING ARRAY)"
            )
            self.execute(
                f"COPY INTO {self.qualified_schema}.RAW FROM (SELECT $1:id::NUMBER, $1:doc_name::VARCHAR, $1:page::NUMBER, $1:embedding::ARRAY FROM @{self.qualified_schema}.UPLOAD) FILE_FORMAT=(TYPE=PARQUET)"
            )
            self.execute(
                f"CREATE TABLE {self.qualified_schema}.CHUNKS CHANGE_TRACKING=TRUE AS SELECT ID, DOC_NAME, PAGE, EMBEDDING::VECTOR(FLOAT,384) AS EMBEDDING FROM {self.qualified_schema}.RAW"
            )
            count = cursor.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT ID) FROM {self.qualified_schema}.CHUNKS"
            ).fetchone()
            if count != (len(chunks), len(chunks)):
                raise ValueError(f"Upload count mismatch: {count}")
            # Verify the complete vector payload survived Parquet/ARRAY/VECTOR conversion.
            uploaded = cursor.execute(
                f"SELECT ID, EMBEDDING::ARRAY FROM {self.qualified_schema}.CHUNKS ORDER BY ID"
            )
            verified = 0
            for idx, array in uploaded:
                values = json.loads(array) if isinstance(array, str) else array
                if not np.array_equal(
                    np.asarray(values, dtype=np.float32), vectors[idx]
                ):
                    raise ValueError(f"Vector round-trip mismatch at {idx}")
                verified += 1
            self.plan["verified_vector_rows"] = verified
            self.execute(f"DROP TABLE {self.qualified_schema}.RAW")
            self.execute(f"DROP STAGE {self.qualified_schema}.UPLOAD")
            path.unlink()
            print(
                "cortex: all vectors verified; building vector-only search index",
                flush=True,
            )
            ddl = (
                f"CREATE CORTEX SEARCH SERVICE {self.qualified_schema}.SEARCH "
                f"VECTOR INDEXES EMBEDDING ATTRIBUTES DOC_NAME "
                f"WAREHOUSE={identifier(warehouse)} TARGET_LAG='1 hour' INITIALIZE=ON_CREATE "
                f"AS SELECT ID, DOC_NAME, PAGE, EMBEDDING FROM {self.qualified_schema}.CHUNKS"
            )
            (directory / "create-service.sql").write_text(ddl + ";\n")
            self.execute(ddl)
            cursor.execute(
                f"DESCRIBE CORTEX SEARCH SERVICE {self.qualified_schema}.SEARCH"
            )
            self.plan["service_description"] = [
                dict(zip([d[0] for d in cursor.description], row))
                for row in cursor.fetchall()
            ]
            self.service = (
                Root(self.connection)
                .databases[database]
                .schemas[self.schema]
                .cortex_search_services["SEARCH"]
            )
            result = self.search(vectors[0])
            if len(result) != 10:
                raise ValueError("Cortex index is not ready with 10 results")
            self.plan["ready_utc"] = datetime.now(timezone.utc).isoformat()
            (directory / "cloud-plan.json").write_text(
                json.dumps(self.plan, indent=2, default=str)
            )
        except BaseException:
            self.close()
            raise

    def execute(self, sql):
        begin = time.perf_counter()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
        finally:
            self.events.append(
                {
                    "sql": sql,
                    "query_id": cursor.sfqid,
                    "seconds": time.perf_counter() - begin,
                }
            )
            (self.directory / "sql-events.json").write_text(
                json.dumps(self.events, indent=2)
            )
            cursor.close()

    def search(self, vector, doc=None):
        response = self.service.search(**vector_query(vector, doc))
        return [int(row["ID"]) for row in response.results]

    def close(self):
        if self.connection is None:
            return
        try:
            if self.schema_created:
                self.execute(f"DROP SCHEMA {self.qualified_schema} CASCADE")
                self.schema_created = False
                (self.directory / "cleanup.json").write_text(
                    json.dumps(
                        {
                            "schema_dropped": self.schema,
                            "completed_utc": datetime.now(timezone.utc).isoformat(),
                            "service_and_source_removed": True,
                            "shared_warehouse_configuration_changed": False,
                        },
                        indent=2,
                    )
                )
            if self.database_created:
                self.execute(f"DROP DATABASE {self.owned_database}")
                self.database_created = False
                cleanup = self.directory / "cleanup.json"
                state = json.loads(cleanup.read_text()) if cleanup.exists() else {}
                state["database_dropped"] = self.owned_database
                cleanup.write_text(json.dumps(state, indent=2))
        finally:
            self.connection.close()
