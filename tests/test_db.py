import time

import pytest

from sqlagent.db import MAX_LINE, QueryError, column_values, describe_table, execute, overview, run, same_result

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


def test_same_result_follows_official_set_semantics():
    assert same_result([(1,), (2,)], [(2,), (1,)])
    assert same_result([(1,), (1,)], [(1,)])
    assert same_result([], [])
    assert not same_result([(1,)], [(2,)])


def test_overview_lists_tables_rows_and_joins(db):
    o = overview(db)
    assert o.startswith('<database name="shop">')
    assert "items (6 rows)" in o and "regions (5 rows)" in o
    assert "order lines.item_id → items.id" in o


def test_describe_table_has_types_keys_and_descriptions(db):
    d = describe_table(db, "ITEMS")
    assert d.splitlines()[0] == "table items:"
    assert "- id INTEGER PRIMARY KEY" in d
    assert "- name TEXT — name of the fruit" in d
    assert '- note TEXT — values: "x" stands for unknown' in d
    assert "- item_id INTEGER → items.id" in describe_table(db, "order lines")


def test_describe_unknown_table_lists_real_ones(db):
    out = describe_table(db, "nope")
    assert out.startswith("ERROR: unknown table 'nope'") and "regions" in out


def test_column_values_search_is_case_insensitive_and_shows_spelling(db):
    out = column_values(db, "Regions", "NAME", search="BOHEMIA")
    assert "'east Bohemia' (2)" in out and "'north Bohemia' (1)" in out and "Prague" not in out


def test_column_values_search_treats_wildcards_and_quotes_literally(db):
    assert "'50%_off' (1)" in column_values(db, "regions", "name", search="%_")
    assert "Bohemia" not in column_values(db, "regions", "name", search="%_")
    assert "(no matching values)" in column_values(db, "regions", "name", search="x' OR '1'='1")


def test_column_values_without_search_lists_top_values_and_distinct_count(db):
    out = column_values(db, "regions", "name")
    assert "(4 distinct)" in out and out.splitlines()[1] == "'east Bohemia' (2)"


def test_column_values_rejects_unknown_column(db):
    assert column_values(db, "regions", "nope").startswith("ERROR: unknown column 'nope'")
