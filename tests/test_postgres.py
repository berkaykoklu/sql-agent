"""Runs against the BIRD PostgreSQL dump (scripts/pg_bird.sh) with the read-only role; skipped without PG_URL."""
import os

import pytest

from sqlagent.db import Database, QueryError

PG_URL = os.environ.get("PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PG_URL to run PostgreSQL tests")


@pytest.fixture(scope="module")
def pg():
    return Database(PG_URL, tables=["account", "district"], timeout=1.0)


def test_scope_and_overview(pg):
    assert pg.table_names == ["account", "district"]
    o = pg.overview()
    assert 'dialect="PostgreSQL"' in o and "account (4,500 rows)" in o and "trans" not in o


def test_describe_and_column_values_are_case_insensitive(pg):
    assert pg.describe_table("DISTRICT").splitlines()[0] == "table district:"
    assert "'east Bohemia'" in pg.column_values("District", "A3", search="BOHEMIA")


def test_writes_and_extra_statements_never_run(pg):
    for sql in ("DELETE FROM account", "SELECT 1; DELETE FROM account", "SET TRANSACTION READ WRITE",
                "WITH x AS (DELETE FROM account RETURNING 1) SELECT * FROM x"):
        with pytest.raises(QueryError):
            pg.execute(sql)
    assert pg.execute("SELECT COUNT(*) FROM account") == [(4500,)]


def test_server_side_timeout(pg):
    with pytest.raises(QueryError, match="timeout"):
        pg.execute("SELECT pg_sleep(3)")


def test_literals_reach_the_server_unchanged(pg):
    assert pg.run("SELECT '1:23%' AS t") == "('1:23%',)"


def test_tool_output_shows_plain_values(pg):
    assert pg.run("SELECT CAST(2.50 AS NUMERIC), DATE '2020-01-02'") == "(2.5, '2020-01-02')"
