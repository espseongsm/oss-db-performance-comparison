## Findings

**Filter execution strategy changed retrieval correctness more than the small latency differences between ANN engines.** In the baseline configuration, DuckDB VSS and ClickHouse applied the document filter to too few ANN candidates. Both returned fewer than the expected `min(10, eligible chunks)` for every filtered request. Tie-aware ANN recall fell to 14.2% and 14.3%, respectively. Those timings should not be used as a successful-search speed ranking.

The follow-up configurations explicitly selected candidates before ranking them: a materialized CTE in DuckDB, and `vector_search_filter_strategy='prefilter'` in ClickHouse. Both reached 100% filtered recall with zero shortfalls. Their filtered p95 latencies were 1.94 ms and 7.34 ms. This is an exact-search fallback over a small candidate set, not evidence of filtered HNSW support. The eligible set comprised 0.005% to 0.857% of the corpus (median 0.260%).

In the initial global-search configurations, ANN p95 latency ranged from 2.33 to 6.71 ms, versus 39.77 ms for DuckDB exact search and 47.60 ms for sqlite-vec exact search. ANN tie-aware recall ranged from 94.9% to 98.1%. These configurations have different quality levels, and native embedded calls differ from Docker server calls; the data does not establish a universal fastest database.

**Exact vector retrieval did not imply successful evidence retrieval.** Both exact baselines found at least one annotated evidence page for 44/150 questions (29.3%) globally, versus 94/150 (62.7%) when the correct source document was supplied. This measures the combined parsing/chunking/embedding pipeline against annotated pages, not LLM answer accuracy. The document-filter result assumes an oracle source document and is not a deployable end-to-end accuracy claim.

## Execution and provenance

- Seven OSS databases, ten configurations including the exact baseline and two prefilter refinements; 20 configuration/mode combinations and 9,000 measured requests. Warm-ups are additional and excluded.
- Host: 14 logical CPU cores, 48 GiB RAM, ARM64 macOS. Each server container had a 4 CPU / 8 GiB limit. Embeddings were generated once on MPS before database measurements.
- [Initial run](../run-20260921-local/manifest.json) and [follow-up run](../refinements-20260921/manifest.json) contain identical input hashes and separate measured-source snapshots. The initial Milvus attempt failed before collecting queries; after replacing its startup check with a successful search-API connection, the follow-up completed. The original failure log is retained.
- Each configuration's ANN index was built once. Follow-up builds used the same global-search parameters but produced slightly different recall. The three query repetitions do not measure index-build variability, and differences between baseline and follow-up global results should not be attributed to prefiltering.
- [Raw-result audit](audit.json) checks the full query/repeat matrix, input hashes, returned-ID validity, document filters, independently recomputed recall/evidence metrics and p95 aggregation. [Summary CSV](summary.csv) retains all metrics; raw per-query files and query plans are linked below through their result directories.
- Snowflake SQL and Cortex Search remain unmeasured pending selection of the cloud connection profile. No cloud cost was incurred by this local experiment.
