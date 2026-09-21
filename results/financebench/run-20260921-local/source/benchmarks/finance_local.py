"""Local SQL backends; all queries return integer chunk IDs."""

import sqlite3

import numpy as np
import pyarrow as pa


def vector_literal(vector):
    return "[" + ",".join(str(float(x)) for x in vector) + "]"


class SQLiteBackend:
    def __init__(self, vectors, chunks, directory):
        import sqlite_vec

        self.db = sqlite3.connect(directory / "sqlite.db")
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.version = self.db.execute("select vec_version()").fetchone()[0]
        self.db.execute(
            f"CREATE VIRTUAL TABLE items USING vec0(id INTEGER PRIMARY KEY, doc TEXT, embedding float[{vectors.shape[1]}] distance_metric=cosine)"
        )
        self.db.executemany(
            "INSERT INTO items VALUES (?,?,?)",
            [(c["id"], c["doc_name"], v.tobytes()) for c, v in zip(chunks, vectors)],
        )
        self.db.commit()
        self.plan = "sqlite-vec vec0 exact KNN; metadata doc filter"

    def search(self, vector, doc=None):
        sql = "SELECT id FROM items WHERE embedding MATCH ? AND k=10"
        params = [np.asarray(vector, dtype="float32").tobytes()]
        if doc is not None:
            sql += " AND doc=?"
            params.append(doc)
        return [r[0] for r in self.db.execute(sql + " ORDER BY distance", params)]

    def close(self):
        self.db.close()


class DuckDBBackend:
    def __init__(self, vectors, chunks, directory, ann=True):
        import duckdb

        self.db = duckdb.connect()
        self.db.execute("SET threads=4")
        self.dim = vectors.shape[1]
        self.version = duckdb.__version__
        arrow_data = pa.table(
            {
                "id": np.arange(len(chunks)),
                "doc": [c["doc_name"] for c in chunks],
                "embedding": pa.FixedSizeListArray.from_arrays(
                    pa.array(vectors.ravel()), self.dim
                ),
            }
        )
        self.db.register("input_data", arrow_data)
        self.db.execute(
            f"CREATE TABLE items AS SELECT id, doc, embedding::FLOAT[{self.dim}] embedding FROM input_data"
        )
        self.db.unregister("input_data")
        if ann:
            self.db.execute("INSTALL vss; LOAD vss")
            self.db.execute(
                "CREATE INDEX idx ON items USING HNSW(embedding) WITH (metric='cosine', M=16, ef_construction=128)"
            )
            self.db.execute("SET hnsw_ef_search=128")
        self.plan = self.db.execute("EXPLAIN " + self.sql(vectors[0])).fetchall()
        self.filtered_plan = self.db.execute(
            "EXPLAIN " + self.sql(vectors[0], chunks[0]["doc_name"])
        ).fetchall()

    def sql(self, vector, doc=None):
        where = "" if doc is None else " WHERE doc='" + doc.replace("'", "''") + "'"
        return f"SELECT id FROM items{where} ORDER BY array_cosine_distance(embedding, {vector_literal(vector)}::FLOAT[{self.dim}]) LIMIT 10"

    def search(self, vector, doc=None):
        return [r[0] for r in self.db.execute(self.sql(vector, doc)).fetchall()]

    def close(self):
        self.db.close()


class PostgresBackend:
    def __init__(self, vectors, chunks, directory):
        import psycopg

        self.db = psycopg.connect(
            "host=localhost port=15432 user=benchmark password=financebench_local dbname=financebench",
            autocommit=True,
        )
        self.db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self.version = self.db.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()[0]
        self.db.execute("DROP TABLE IF EXISTS fb_items")
        self.db.execute(
            f"CREATE TABLE fb_items(id bigint PRIMARY KEY, doc text, embedding vector({vectors.shape[1]}))"
        )
        with self.db.cursor().copy("COPY fb_items FROM STDIN") as copy:
            for c, v in zip(chunks, vectors):
                copy.write_row((c["id"], c["doc_name"], vector_literal(v)))
        self.db.execute("SET maintenance_work_mem='512MB'")
        self.db.execute(
            "CREATE INDEX ON fb_items USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=128)"
        )
        self.db.execute("CREATE INDEX ON fb_items(doc)")
        self.db.execute("ANALYZE fb_items")
        self.db.execute("SET hnsw.ef_search=128; SET hnsw.iterative_scan=strict_order")
        self.plan = self.db.execute(
            "EXPLAIN SELECT id FROM fb_items ORDER BY embedding <=> %s::vector LIMIT 10",
            (vector_literal(vectors[0]),),
        ).fetchall()
        self.filtered_plan = self.db.execute(
            "EXPLAIN SELECT id FROM fb_items WHERE doc=%s ORDER BY embedding <=> %s::vector LIMIT 10",
            (chunks[0]["doc_name"], vector_literal(vectors[0])),
        ).fetchall()

    def search(self, vector, doc=None):
        where, params = ("", []) if doc is None else (" WHERE doc=%s", [doc])
        params.append(vector_literal(vector))
        return [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM fb_items"
                + where
                + " ORDER BY embedding <=> %s::vector LIMIT 10",
                params,
            ).fetchall()
        ]

    def close(self):
        self.db.close()


class ClickHouseBackend:
    def __init__(self, vectors, chunks, directory):
        import clickhouse_connect

        self.db = clickhouse_connect.get_client(
            host="localhost",
            port=18123,
            username="benchmark",
            password="financebench_local",
            database="financebench",
        )
        self.version = self.db.command("SELECT version()")
        self.db.command("DROP TABLE IF EXISTS fb_items")
        self.db.command(
            f"CREATE TABLE fb_items(id UInt64, doc String, embedding Array(Float32), INDEX idx embedding TYPE vector_similarity('hnsw', 'cosineDistance', {vectors.shape[1]}, 'f32', 16, 128) GRANULARITY 100000000) ENGINE=MergeTree ORDER BY id"
        )
        for start in range(0, len(chunks), 2000):
            self.db.insert(
                "fb_items",
                [
                    [c["id"], c["doc_name"], v.tolist()]
                    for c, v in zip(
                        chunks[start : start + 2000], vectors[start : start + 2000]
                    )
                ],
                column_names=["id", "doc", "embedding"],
            )
        self.db.command("OPTIMIZE TABLE fb_items FINAL")
        self.plan = self.db.query(
            "EXPLAIN indexes=1 " + self.sql(vectors[0])
        ).result_rows
        self.filtered_plan = self.db.query(
            "EXPLAIN indexes=1 " + self.sql(vectors[0], chunks[0]["doc_name"])
        ).result_rows

    def sql(self, vector, doc=None):
        where = "" if doc is None else " WHERE doc='" + doc.replace("'", "\\'") + "'"
        return f"SELECT id FROM fb_items{where} ORDER BY cosineDistance(embedding, {vector_literal(vector)}) LIMIT 10 SETTINGS max_threads=4, hnsw_candidate_list_size_for_search=128"

    def search(self, vector, doc=None):
        return [r[0] for r in self.db.query(self.sql(vector, doc)).result_rows]

    def close(self):
        self.db.close()
