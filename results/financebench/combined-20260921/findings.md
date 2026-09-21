## Cortex Search: measured with the same embeddings

**Cortex Search reused all 115,174 BGE document vectors and all 150 query vectors without generating embeddings in Snowflake.** The service description confirms `embedding_model=user_provided_vectors`, `auto_embedded=false`, and a vector-only index. All uploaded vectors were read back and matched exactly to the local float32 arrays. Only vector queries were sent, with keyword weight zero and reranking disabled; results contained chunk IDs only.

| Cortex condition | p50 ms | p95 ms | p99 ms | Tie-aware recall@10 | Evidence hit@10 | Result shortfalls |
|---|---:|---:|---:|---:|---:|---:|
| Global | 90.77 | 173.68 | 674.31 | 99.53% | 29.33% | 0 / 450 |
| Oracle document filter | 89.11 | 174.54 | 316.91 | 99.67% | 62.67% | 0 / 450 |

The exact baselines achieved 29.33% global and 62.67% oracle-filter evidence hit@10. Cortex's evidence results should be interpreted against those fixed-embedding reference results for this dataset, separately from its ANN recall. The oracle filter supplies the correct source document and is not an end-to-end document-selection result.

**Cloud and localhost latency are different deployment measurements.** The Cortex client was the same ARM64 Mac, but requests travelled over HTTPS to `AWS_AP_NORTHEAST_2` (Seoul). Snowflake version 10.33.101 and Python API snowflake-core 1.13.1 were used. The X-Small warehouse handled source/index work; Cortex serving compute is managed separately. Server-only latency, matched hardware and equivalent ANN tuning were not measured. A larger Cortex latency number does not establish that its ANN engine is intrinsically slower.

Successful setup took 81.59 seconds including authentication, upload, full vector read-back validation and index readiness. The CREATE SEARCH SERVICE statement took 14.92 seconds. This total includes an additional full-payload validation step compared with the OSS runs and should not be ranked as equivalent index-build time.

The cloud extension completed 900 measured requests plus 300 warm-ups and one untimed readiness query. Together with the preserved OSS runs, this report contains 11 configurations across seven OSS databases and Cortex Search: 9,900 measured requests. The input hashes match across all source runs. The experiment database, schema, table and service were deleted after measurement; shared warehouse settings were not changed. Billing totals were not measured, and deletion does not imply zero charges or immediate removal of retained storage.

Two attempts ended before timed queries: the configured personal database rejected ordinary tables; a later attempt completed indexing but encountered datetime metadata serialization. Both were cleaned up. Their logs remain in `../cortex-20260921/` and `../cortex-20260921-retry/`. The successful run uses a separate experiment database and includes a tested cleanup path for metadata-write failures.

Cortex protocol: [DDL and supplied vectors](https://docs.snowflake.com/en/sql-reference/sql/create-cortex-search), [vector query API](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/query-cortex-search-service), [reranking controls](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-customize-scoring). Serving and warehouse charges are distinct in [Snowflake's cost documentation](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-costs).

## Preserved local findings

**Filter execution strategy changed retrieval correctness more than the small latency differences between ANN engines.** In the baseline configuration, DuckDB VSS and ClickHouse applied the document filter to too few ANN candidates. Both returned fewer than the expected `min(10, eligible chunks)` for every filtered request. Tie-aware ANN recall fell to 14.2% and 14.3%, respectively. Those timings should not be used as a successful-search speed ranking.

The follow-up configurations explicitly selected candidates before ranking them: a materialized CTE in DuckDB, and `vector_search_filter_strategy='prefilter'` in ClickHouse. Both reached 100% filtered recall with zero shortfalls. Their filtered p95 latencies were 1.94 ms and 7.34 ms. This is an exact-search fallback over a small candidate set, not evidence of filtered HNSW support. The eligible set comprised 0.005% to 0.857% of the corpus (median 0.260%).

In the initial global-search configurations, ANN p95 latency ranged from 2.33 to 6.71 ms, versus 39.77 ms for DuckDB exact search and 47.60 ms for sqlite-vec exact search. ANN tie-aware recall ranged from 94.9% to 98.1%. These configurations have different quality levels, and native embedded calls differ from Docker server calls; the data does not establish a universal fastest database.

**Exact vector retrieval did not imply successful evidence retrieval.** Both exact baselines found at least one annotated evidence page for 44/150 questions (29.3%) globally, versus 94/150 (62.7%) when the correct source document was supplied. This measures the combined parsing/chunking/embedding pipeline against annotated pages, not LLM answer accuracy. The document-filter result assumes an oracle source document and is not a deployable end-to-end accuracy claim.

## Local execution and provenance

- Seven OSS databases, ten configurations including the exact baseline and two prefilter refinements; 20 configuration/mode combinations and 9,000 measured requests. Warm-ups are additional and excluded.
- Host: 14 logical CPU cores, 48 GiB RAM, ARM64 macOS. Each server container had a 4 CPU / 8 GiB limit. Embeddings were generated once on MPS before database measurements.
- [Initial run](../run-20260921-local/manifest.json) and [follow-up run](../refinements-20260921/manifest.json) contain identical input hashes and separate measured-source snapshots. The initial Milvus attempt failed before collecting queries; after replacing its startup check with a successful search-API connection, the follow-up completed. The original failure log is retained.
- Each configuration's ANN index was built once. Follow-up builds used the same global-search parameters but produced slightly different recall. The three query repetitions do not measure index-build variability, and differences between baseline and follow-up global results should not be attributed to prefiltering.
- [Combined raw-result audit](audit.json) checks the full query/repeat matrix, input hashes, returned-ID validity, document filters, independently recomputed recall/evidence metrics and p95 aggregation. [Summary CSV](summary.csv) retains all metrics; raw per-query files and query plans are linked below through their result directories.
- The local runs incurred no Snowflake cost. The separately measured Cortex extension is described above; Snowflake SQL exact search remains unmeasured.
