"""Validate Doris data semantics and isolation before a live database run."""

import argparse
import json
import re
import sys
from types import SimpleNamespace

import duckdb
import pytest

import main
from scripts import doris_sql, engine_worker


def test_reference_validation_fails_before_timing_on_value_mismatch(tmp_path):
    reference = {
        "rows": 100,
        "queries": {"small": {"row_count": 1, "digest": "correct"}},
    }
    path = tmp_path / "validation.json"
    path.write_text(json.dumps(reference))
    assert doris_sql.verify_reference(reference, path)["status"] == "passed"
    different_size = {**reference, "rows": 10}
    assert (
        doris_sql.verify_reference(different_size, path)["status"]
        == "skipped_rows_mismatch"
    )
    invalid = {
        "rows": 100,
        "queries": {"small": {"row_count": 1, "digest": "incorrect"}},
    }
    with pytest.raises(RuntimeError, match="DuckDB"):
        doris_sql.verify_reference(invalid, path)


def duckdb_generated(sql):
    """Evaluate deterministic expressions after substituting Doris TVF syntax."""
    sql = re.sub(
        r"numbers\('number'='(\d+)'\)",
        lambda match: f"range(0, {match[1]}) AS generated(number)",
        sql,
    )
    return sql.replace(" DIV ", " // ")


@pytest.mark.parametrize("start", [0, 9, 999, 1_824, 999_999, 987_654_319])
def test_generated_rows_match_logical_reference(start):
    with duckdb.connect() as connection:
        actual = connection.execute(
            duckdb_generated(engine_worker.generated_select("doris", start, start + 3))
        ).fetchall()
        dimensions = connection.execute(
            duckdb_generated(
                engine_worker.generated_dimension_select("doris", start, start + 3)
            )
        ).fetchall()
    assert actual == [engine_worker.row_values(i) for i in range(start, start + 3)]
    assert dimensions == [
        engine_worker.dimension_row_values(i) for i in range(start, start + 3)
    ]


def test_query_results_keep_reference_checksums():
    rows = 2_000
    medium, large, join = {}, {}, {}
    for i in range(rows):
        cents, discount = i % 100_000, i % 20
        group = large.setdefault(i % 1000, [0, 0, 0])
        group[0] += 1
        group[1] += cents
        group[2] += i % 100 + 1
        group = join.setdefault((i % 50, (i // 1000) % 5), [0, 0])
        group[0] += 1
        group[1] += cents
        if 600 <= i % 1825 <= 699:
            group = medium.setdefault(i % 50, [0, 0, 0])
            group[0] += 1
            group[1] += cents
            group[2] += discount
    expected = {
        "small": [(1999, 1999, 999, 19.99)],
        "medium": [
            (key, count, cents / 100, round(discount / count / 100, 6))
            for key, (count, cents, discount) in sorted(medium.items())
        ],
        "large": [
            (key, count, cents / 100, quantity)
            for key, (count, cents, quantity) in sorted(large.items())
        ],
        "join": [
            (*key, count, cents / 100) for key, (count, cents) in sorted(join.items())
        ],
    }
    with duckdb.connect() as connection:
        connection.execute(
            f"CREATE TABLE benchmark AS {engine_worker.generated_select('duckdb', 0, rows)}"
        )
        connection.execute(
            "CREATE TABLE account_dim AS "
            f"{engine_worker.generated_dimension_select('duckdb', 0, rows)}"
        )
        for doris in engine_worker.query_specs(rows, "doris"):
            actual = connection.execute(doris.sql).fetchall()
            assert engine_worker.digest_rows(actual) == engine_worker.digest_rows(
                expected[doris.name]
            )


class Cursor:
    description = None

    def close(self):
        pass

    def fetchall(self):
        return []


class Connection:
    def __init__(self):
        self.statements = []

    def execute(self, sql):
        self.statements.append(sql)
        return Cursor()

    def commit(self):
        pass

    def metadata(self):
        return {"version": "doris-test", "mysql_compatibility_version": "5.7.99"}


def test_deployment_captures_actual_limits_and_image(tmp_path, monkeypatch):
    container = {
        "Id": "container-id",
        "Image": "sha256:actual-image",
        "Config": {
            "Image": "apache/doris:all-in-one-4.1.3",
            "Labels": {"com.docker.compose.service": "doris"},
            "Env": ["DORIS_PASSWORD=secret"],
        },
        "HostConfig": {"NanoCpus": 14_000_000_000, "Memory": 24 * 2**30},
    }
    image = {
        "Id": "sha256:actual-image",
        "Architecture": "amd64",
        "Os": "linux",
        "RepoDigests": ["apache/doris@sha256:immutable"],
    }
    daemon = {"NCPU": 14, "MemTotal": 26 * 2**30, "Architecture": "aarch64"}
    responses = [
        "container-id\n",
        json.dumps([container]),
        json.dumps([image]),
        json.dumps(daemon),
    ]
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=responses.pop(0)),
    )
    output = tmp_path / "new-run"
    main.record_deployment(output, ("doris",))
    snapshot = json.loads((output / "deployment.json").read_text())
    assert snapshot["docker"]["NCPU"] == 14
    assert snapshot["containers"][0]["limits"]["Memory"] == 24 * 2**30
    assert snapshot["images"][0]["RepoDigests"] == ["apache/doris@sha256:immutable"]
    assert snapshot["images"][0]["Architecture"] == "amd64"
    assert "Env" not in snapshot["containers"][0]


@pytest.mark.parametrize("optimized", [False, True])
def test_physical_design_preserves_insert_column_mapping(optimized):
    connection = Connection()
    metadata = doris_sql.setup(
        connection,
        "benchmark",
        "account_dim",
        5,
        3,
        2,
        optimized,
        engine_worker.generated_select,
        engine_worker.generated_dimension_select,
    )
    fact_ddl = connection.statements[1]
    assert (
        "DUPLICATE KEY(event_day, id)" in fact_ddl
        if optimized
        else ("DUPLICATE KEY(id)" in fact_ddl)
    )
    assert ('"bloom_filter_columns" = "id"' in fact_ddl) == optimized
    fact_inserts = [
        sql for sql in connection.statements if sql.startswith("INSERT INTO benchmark")
    ]
    assert len(fact_inserts) == 3
    assert all(
        sql.startswith(f"INSERT INTO benchmark ({', '.join(doris_sql.FACT_COLUMNS)})")
        for sql in fact_inserts
    )
    assert "(number + 4) AS id" in fact_inserts[-1]
    assert "prefix index" in metadata["built_in_indexes"]
    assert metadata["replication_num"] == 1
    assert metadata["session"]["version"] == "doris-test"
    assert metadata["session"]["mysql_compatibility_version"] == "5.7.99"


def args():
    return argparse.Namespace(output_dir=None, pilot=False)


def test_doris_is_optional_and_pilots_are_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    assert "doris" in main.AVAILABLE_ENGINES
    assert "doris" not in main.ENGINES
    baseline = main.select_result_root(args(), main.ENGINES)
    pilot = main.select_result_root(
        argparse.Namespace(output_dir=None, pilot=True), main.ENGINES
    )
    doris = main.select_result_root(args(), ("doris",))
    assert baseline == tmp_path / "results"
    assert pilot.parent == baseline / "sql"
    assert doris.parent == baseline / "doris-sql"
    assert not pilot.exists() and not doris.exists()


@pytest.mark.parametrize("target", ["results", "results/clickhouse", "outside"])
def test_invalid_output_rejected_before_docker(monkeypatch, tmp_path, target):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    (tmp_path / "results" / "clickhouse").mkdir(parents=True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "--engine", "doris", "--output-dir", str(tmp_path / target)],
    )
    monkeypatch.setattr(
        main, "ensure_docker", lambda: pytest.fail("Docker must not run")
    )
    with pytest.raises(SystemExit, match="output-dir"):
        main.main()


def test_worker_maps_new_output_directory(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "compose", lambda *arguments: calls.append(arguments))
    output = main.ROOT / "results" / "doris-sql" / "fresh-run"
    main.worker(
        "runner", "doris", "measure", 10_000, 1, "optimized", result_root=output
    )
    assert "BENCH_RESULTS=/results/doris-sql/fresh-run/optimized" in calls[0]
    assert main.services_for("doris") == ("runner", "doris")


def test_help_requires_no_docker(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])
    monkeypatch.setattr(
        main, "ensure_docker", lambda: pytest.fail("Docker must not run")
    )
    with pytest.raises(SystemExit) as exit_code:
        main.main()
    assert exit_code.value.code == 0
    assert "doris" in capsys.readouterr().out
