import gc, os, platform, sys, time
import duckdb, numpy as np, pandas as pd, psutil, pyarrow as pa
WORKLOADS = {'filter_project': ['id', 'region_id', 'amount_cents', 'quantity'], 'clean_derive': ['id', 'amount_cents', 'quantity', 'discount_pct'], 'groupby': ['region_id', 'amount_cents', 'quantity'], 'join_groupby': ['account_id', 'region_id', 'amount_cents']}
FILTERS = [('region_id', '<', 10), ('amount_cents', '>=', 1000)]
def pandas_transform(
    frame: pd.DataFrame, accounts: pd.DataFrame | None, workload: str
) -> pd.DataFrame:
    if workload == "filter_project":
        selected = frame.loc[(frame.region_id < 10) & (frame.amount_cents >= 1000)]
        return pd.DataFrame(
            {
                "id": selected.id,
                "region_id": selected.region_id,
                "gross_cents": selected.amount_cents * selected.quantity,
            }
        ).reset_index(drop=True)
    if workload == "clean_derive":
        discount = frame.discount_pct.fillna(0).astype("int64")
        return pd.DataFrame(
            {
                "id": frame.id,
                "net_cents": frame.amount_cents
                * frame.quantity
                * (100 - discount)
                // 100,
            }
        )
    if workload == "groupby":
        return frame.groupby("region_id", sort=False, as_index=False).agg(
            total_cents=("amount_cents", "sum"),
            total_quantity=("quantity", "sum"),
            row_count=("amount_cents", "size"),
        )
    if workload == "join_groupby":
        joined = frame.merge(accounts, on="account_id", how="inner", sort=False)
        return joined.groupby(["region_id", "tier"], sort=False, as_index=False).agg(
            total_cents=("amount_cents", "sum"),
            row_count=("amount_cents", "size"),
        )
    raise ValueError(workload)


def sql_query(workload: str) -> str:
    queries = {
        "filter_project": """
            SELECT id, region_id, amount_cents * quantity AS gross_cents
            FROM sales WHERE region_id < 10 AND amount_cents >= 1000
        """,
        "clean_derive": """
            SELECT id, amount_cents * quantity *
                (100 - CAST(COALESCE(discount_pct, 0) AS BIGINT)) // 100 AS net_cents
            FROM sales
        """,
        "groupby": """
            SELECT region_id, CAST(SUM(amount_cents) AS BIGINT) AS total_cents,
                CAST(SUM(quantity) AS BIGINT) AS total_quantity, COUNT(*) AS row_count
            FROM sales GROUP BY region_id
        """,
        "join_groupby": """
            SELECT region_id, tier, CAST(SUM(amount_cents) AS BIGINT) AS total_cents,
                COUNT(*) AS row_count
            FROM sales JOIN accounts USING (account_id) GROUP BY region_id, tier
        """,
    }
    return queries[workload]


def fingerprint(frame: pd.DataFrame) -> dict:
    """Order-independent full-result row hashes; never included in elapsed time."""
    values = pd.util.hash_pandas_object(frame, index=False).to_numpy()
    return {
        "rows": len(frame),
        "columns": list(frame.columns),
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "hash_sum": str(int(values.sum(dtype=np.uint64))),
        "hash_xor": str(int(np.bitwise_xor.reduce(values, initial=np.uint64(0)))),
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


def input_frame(session, table, columns, filtered=False):
    query = f"SELECT {', '.join(columns)} FROM {table}"
    if filtered:
        query += " WHERE " + " AND ".join(f"{c} {op} {v}" for c, op, v in FILTERS)
    frame = session.sql(query).to_pandas()
    frame.columns = [c.lower() for c in frame.columns]
    return frame.astype(
        {c: "float64" if c == "discount_pct" else "int64" for c in columns}
    )


def sample(session, config):
    engine, work = config["engine"], config["workload"]
    fact, dimension = config["fact"], config["dimension"]
    connection = None
    gc.collect()
    try:
        with session.query_history() as history:
            cpu_start, start = time.process_time_ns(), time.perf_counter_ns()
            if engine == "snowflake":
                loaded = prepared = start
                output = session.sql(snowflake_query(work, fact, dimension)).to_pandas()
            else:
                frame = input_frame(
                    session, fact, WORKLOADS[work], work == "filter_project"
                )
                accounts = (
                    input_frame(session, dimension, ["account_id", "tier"])
                    if work == "join_groupby"
                    else None
                )
                loaded = time.perf_counter_ns()
                if engine == "duckdb":
                    connection = duckdb.connect(
                        config={
                            "threads": config["threads"],
                            "memory_limit": f"{config['memory_gb']}GB",
                        }
                    )
                    connection.register("sales", frame)
                    if accounts is not None:
                        connection.register("accounts", accounts)
                    prepared = time.perf_counter_ns()
                    output = connection.sql(sql_query(work)).df()
                elif engine == "pandas":
                    prepared = loaded
                    output = pandas_transform(frame, accounts, work)
                else:
                    raise ValueError(f"Unknown engine: {engine}")
            output = normalize_result(output, [])
            finished, cpu_finished = time.perf_counter_ns(), time.process_time_ns()
        actual = fingerprint(output)
        if actual != config["expected"]:
            raise ValueError(f"Full-result fingerprint mismatch: {engine}/{work}")
        return {
            "elapsed_ms": (finished - start) / 1e6,
            "input_ms": (loaded - start) / 1e6 if engine != "snowflake" else None,
            "setup_ms": (prepared - loaded) / 1e6,
            "compute_to_dataframe_ms": (finished - prepared) / 1e6,
            "cpu_ms": (cpu_finished - cpu_start) / 1e6,
            "output_rows": len(output),
            "output_bytes": int(output.memory_usage(index=True, deep=True).sum()),
            "fingerprint": actual,
            "validated": True,
            "query_ids": [q.query_id for q in history.queries],
        }
    finally:
        if connection is not None:
            connection.close()


def runtime_info():
    import snowflake.snowpark

    return {
        "python": sys.version,
        "platform": platform.platform(),
        "visible_cpu_count": os.cpu_count(),
        "visible_memory_bytes": psutil.virtual_memory().total,
        "process_rss_bytes": psutil.Process().memory_info().rss,
        "versions": {
            "pandas": pd.__version__,
            "duckdb": duckdb.__version__,
            "numpy": np.__version__,
            "pyarrow": pa.__version__,
            "psutil": psutil.__version__,
            "snowflake-snowpark-python": snowflake.snowpark.__version__,
        },
        "resource_caveat": "Visible container resources do not establish equal per-engine CPU or RAM allocation.",
    }


def run(session, config):
    pa.set_cpu_count(config["threads"])
    pd.set_option("compute.use_numexpr", False)
    runtime = runtime_info()
    runtime["warehouse"] = session.get_current_warehouse()
    cache = session.sql("SHOW PARAMETERS LIKE 'USE_CACHED_RESULT' IN SESSION").collect()
    runtime["use_cached_result"] = cache[0]["value"]
    if runtime["use_cached_result"].lower() != "false":
        raise ValueError("Result cache must be disabled")
    sample(
        session, config
    )  # One unmeasured warmup, also checked against the reference.
    measurements = [sample(session, config) for _ in range(config["runs"])]
    return {"runtime": runtime, "measurements": measurements}
