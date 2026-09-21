"""Dedicated vector server adapters using externally supplied embeddings."""

import time
import uuid


class QdrantBackend:
    def __init__(self, vectors, chunks, directory):
        from qdrant_client import QdrantClient, models

        self.models = models
        self.db = QdrantClient(url="http://localhost:16333", timeout=120)
        self.version = self.db.info().version
        if self.db.collection_exists("financebench"):
            self.db.delete_collection("financebench")
        self.db.create_collection(
            "financebench",
            vectors_config=models.VectorParams(
                size=vectors.shape[1], distance=models.Distance.COSINE
            ),
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=128),
            optimizers_config=models.OptimizersConfigDiff(indexing_threshold=1000),
        )
        self.db.create_payload_index(
            "financebench", "doc", models.PayloadSchemaType.KEYWORD, wait=True
        )
        for start in range(0, len(chunks), 1000):
            self.db.upsert(
                "financebench",
                points=models.Batch(
                    ids=[c["id"] for c in chunks[start : start + 1000]],
                    vectors=vectors[start : start + 1000].tolist(),
                    payloads=[
                        {"doc": c["doc_name"]} for c in chunks[start : start + 1000]
                    ],
                ),
                wait=True,
            )
        deadline = time.monotonic() + 600
        while True:
            info = self.db.get_collection("financebench")
            enough_indexed = (
                len(chunks) < 2000 or info.indexed_vectors_count >= len(chunks) * 0.95
            )
            if info.status == models.CollectionStatus.GREEN and enough_indexed:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Qdrant indexing timeout")
            time.sleep(1)
        if info.points_count != len(chunks):
            raise ValueError("Qdrant point count mismatch")
        self.plan = {
            "hnsw_ef": 128,
            "indexed_vectors_count": info.indexed_vectors_count,
            "points_count": info.points_count,
            "collection_status": str(info.status),
        }

    def search(self, vector, doc=None):
        m = self.models
        condition = (
            None
            if doc is None
            else m.Filter(
                must=[m.FieldCondition(key="doc", match=m.MatchValue(value=doc))]
            )
        )
        result = self.db.query_points(
            "financebench",
            query=vector.tolist(),
            query_filter=condition,
            search_params=m.SearchParams(hnsw_ef=128),
            limit=10,
            with_payload=False,
            with_vectors=False,
        )
        return [int(p.id) for p in result.points]

    def close(self):
        self.db.close()


class WeaviateBackend:
    def __init__(self, vectors, chunks, directory):
        import weaviate
        from weaviate.classes.config import (
            Configure,
            DataType,
            Property,
            VectorDistances,
        )
        from weaviate.classes.init import AdditionalConfig, Timeout

        self.db = weaviate.connect_to_local(
            port=18080,
            grpc_port=15051,
            additional_config=AdditionalConfig(
                timeout=Timeout(init=60, query=60, insert=180)
            ),
        )
        self.version = self.db.get_meta()["version"]
        if self.db.collections.exists("FinanceChunk"):
            self.db.collections.delete("FinanceChunk")
        self.collection = self.db.collections.create(
            "FinanceChunk",
            vector_config=Configure.Vectors.self_provided(
                vector_index_config=Configure.VectorIndex.hnsw(
                    distance_metric=VectorDistances.COSINE,
                    ef=128,
                    ef_construction=128,
                    max_connections=16,
                )
            ),
            properties=[
                Property(name="chunk_id", data_type=DataType.INT),
                Property(
                    name="doc",
                    data_type=DataType.TEXT,
                    tokenization=weaviate.classes.config.Tokenization.FIELD,
                ),
            ],
        )
        with self.collection.batch.fixed_size(
            batch_size=256, concurrent_requests=2
        ) as batch:
            for c, v in zip(chunks, vectors):
                batch.add_object(
                    properties={"chunk_id": c["id"], "doc": c["doc_name"]},
                    vector=v.tolist(),
                    uuid=uuid.UUID(int=c["id"] + 1),
                )
        if self.collection.batch.failed_objects:
            raise RuntimeError(
                f"Weaviate failed inserts: {len(self.collection.batch.failed_objects)}"
            )
        count = self.collection.aggregate.over_all(total_count=True).total_count
        if count != len(chunks):
            raise ValueError(f"Weaviate count mismatch {count}")
        self.plan = "HNSW cosine M=16 efConstruction=128 ef=128; synchronous indexing"

    def search(self, vector, doc=None):
        from weaviate.classes.query import Filter

        condition = None if doc is None else Filter.by_property("doc").equal(doc)
        result = self.collection.query.near_vector(
            near_vector=vector.tolist(),
            limit=10,
            filters=condition,
            return_properties=["chunk_id"],
        )
        return [int(r.properties["chunk_id"]) for r in result.objects]

    def close(self):
        self.db.close()


class MilvusBackend:
    def __init__(self, vectors, chunks, directory):
        from pymilvus import DataType, MilvusClient

        self.db = MilvusClient(uri="http://localhost:19530")
        self.version = self.db.get_server_version()
        if self.db.has_collection("financebench"):
            self.db.drop_collection("financebench")
        schema = self.db.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("doc", DataType.VARCHAR, max_length=512)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=vectors.shape[1])
        self.db.create_collection(
            "financebench", schema=schema, consistency_level="Strong"
        )
        for start in range(0, len(chunks), 1000):
            self.db.insert(
                "financebench",
                [
                    {"id": c["id"], "doc": c["doc_name"], "embedding": v.tolist()}
                    for c, v in zip(
                        chunks[start : start + 1000], vectors[start : start + 1000]
                    )
                ],
            )
        self.db.flush("financebench")
        index = self.db.prepare_index_params()
        index.add_index(
            field_name="embedding",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 128},
        )
        index.add_index(field_name="doc", index_type="INVERTED")
        self.db.create_index("financebench", index)
        self.db.load_collection("financebench")
        self.plan = "HNSW cosine M=16 efConstruction=128 ef=128; Strong consistency"

    def search(self, vector, doc=None):
        import json

        condition = "" if doc is None else "doc == " + json.dumps(doc)
        result = self.db.search(
            "financebench",
            data=[vector.tolist()],
            filter=condition,
            limit=10,
            search_params={"metric_type": "COSINE", "params": {"ef": 128}},
        )
        return [int(r["id"]) for r in result[0]]

    def close(self):
        self.db.close()
