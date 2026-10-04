import time

import pytest

from sqlagent.db import MAX_LINE, QueryError, execute, run, same_result, schema

COUNT_TO_25 = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<25) SELECT x FROM c"


def test_execute_returns_all_rows(db):
    assert execute(db, "SELECT id FROM items ORDER BY id") == [(i,) for i in range(1, 7)]


def test_read_only_rejects_writes(db):
    with pytest.raises(QueryError, match="readonly"):
        execute(db, "DELETE FROM items")
    assert execute(db, "SELECT count(*) FROM items") == [(6,)]


def test_multiple_statements_rejected(db):
    with pytest.raises(QueryError):
        execute(db, "SELECT 1; DELETE FROM items")


def test_timeout_interrupts_long_query(db):
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    start = time.monotonic()
    with pytest.raises(QueryError, match="timeout"):
        execute(db, endless, timeout=0.2)
    assert time.monotonic() - start < 2


def test_non_utf8_text_is_replaced_not_fatal(db):
    assert execute(db, "SELECT note FROM items WHERE id = 5") == [("�",)]


def test_run_formats_rows_errors_and_empty(db):
    assert run(db, "SELECT id FROM items WHERE id < 3 ORDER BY id") == "(1,)\n(2,)"
    assert run(db, "SELECT nope FROM items").startswith("ERROR: no such column")
    assert run(db, "SELECT id FROM items WHERE id > 99") == "(0 rows)"


def test_run_caps_rows_and_line_width(db):
    lines = run(db, COUNT_TO_25).splitlines()
    assert len(lines) == 21 and lines[-1] == "... (25 rows total)"
    assert len(run(db, "SELECT note FROM items WHERE id = 6")) <= MAX_LINE + 3


def test_schema_has_create_statements_and_three_samples(db):
    s = schema(db)
    assert "CREATE TABLE items" in s and 'CREATE TABLE "order lines"' in s
    assert "(1, 'apple', 'red')" in s
    assert "(4, 'kiwi'" not in s


def test_same_result_follows_official_set_semantics():
    assert same_result([(1,), (2,)], [(2,), (1,)])
    assert same_result([(1,), (1,)], [(1,)])
    assert same_result([], [])
    assert not same_result([(1,)], [(2,)])
