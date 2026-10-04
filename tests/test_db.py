import time

import pytest

from sqlagent.db import MAX_LINE, Database, QueryError, same_result

COUNT_TO_25 = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<25) SELECT x FROM c"


def test_execute_returns_all_rows(database):
    assert database.execute("SELECT id FROM items ORDER BY id") == [(i,) for i in range(1, 7)]


def test_read_only_rejects_writes(database):
    with pytest.raises(QueryError, match="readonly|not authorized"):
        database.execute("DELETE FROM items")
    assert database.execute("SELECT count(*) FROM items") == [(6,)]


def test_multiple_statements_rejected(database):
    with pytest.raises(QueryError):
        database.execute("SELECT 1; DELETE FROM items")


def test_timeout_interrupts_long_query(db):
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    start = time.monotonic()
    with pytest.raises(QueryError, match="timeout"):
        Database.sqlite(db, timeout=0.2).execute(endless)
    assert time.monotonic() - start < 2


def test_non_utf8_text_is_replaced_not_fatal(database):
    assert database.execute("SELECT note FROM items WHERE id = 5") == [("�",)]


def test_run_formats_rows_errors_and_empty(database):
    assert database.run("SELECT id FROM items WHERE id < 3 ORDER BY id") == "(1,)\n(2,)"
    assert database.run("SELECT nope FROM items").startswith("ERROR: no such column")
    assert database.run("SELECT id FROM items WHERE id > 99") == "(0 rows)"


def test_run_caps_rows_and_line_width(database):
    lines = database.run(COUNT_TO_25).splitlines()
    assert len(lines) == 21 and lines[-1] == "... (25 rows total)"
    assert len(database.run("SELECT note FROM items WHERE id = 6")) <= MAX_LINE + 3


def test_literals_with_colons_and_percent_reach_the_driver_unchanged(database):
    assert database.run("SELECT '1:23%' AS t") == "('1:23%',)"


def test_same_result_follows_official_set_semantics():
    assert same_result([(1,), (2,)], [(2,), (1,)])
    assert same_result([(1,), (1,)], [(1,)])
    assert same_result([], [])
    assert not same_result([(1,)], [(2,)])


def test_overview_lists_tables_rows_and_joins(database):
    o = database.overview()
    assert o.startswith('<database name="shop" dialect="SQLite">')
    assert "items (6 rows)" in o and "regions (5 rows)" in o
    assert "order lines.item_id → items.id" in o


def test_describe_table_has_types_keys_and_descriptions(database):
    d = database.describe_table("ITEMS")
    assert d.splitlines()[0] == "table items:"
    assert "- id INTEGER PRIMARY KEY" in d
    assert "- name TEXT — name of the fruit" in d
    assert '- note TEXT — values: "x" stands for unknown' in d
    assert "- item_id INTEGER → items.id" in database.describe_table("order lines")


def test_describe_unknown_table_lists_real_ones(database):
    out = database.describe_table("nope")
    assert out.startswith("ERROR: unknown table 'nope'") and "regions" in out


def test_table_scope_hides_other_tables(db):
    scoped = Database.sqlite(db, tables=["ITEMS", "order lines"])
    assert scoped.table_names == ["items", "order lines"]
    assert "regions" not in scoped.overview()
    assert scoped.describe_table("regions").startswith("ERROR: unknown table 'regions'")
    assert scoped.column_values("regions", "name").startswith("ERROR: unknown table")


def test_column_values_search_is_case_insensitive_and_shows_spelling(database):
    out = database.column_values("Regions", "NAME", search="BOHEMIA")
    assert "'east Bohemia' (2)" in out and "'north Bohemia' (1)" in out and "Prague" not in out


def test_column_values_search_treats_wildcards_and_quotes_literally(database):
    assert "'50%_off' (1)" in database.column_values("regions", "name", search="%_")
    assert "Bohemia" not in database.column_values("regions", "name", search="%_")
    assert "(no matching values)" in database.column_values("regions", "name", search="x' OR '1'='1")


def test_column_values_without_search_lists_top_values_and_distinct_count(database):
    out = database.column_values("regions", "name")
    assert "(4 distinct)" in out and out.splitlines()[1] == "'east Bohemia' (2)"


def test_column_values_handles_names_with_spaces_and_brackets(database):
    out = database.column_values("schools", "free meal (k-12)")
    assert out.splitlines()[0] == "most common values of schools.Free Meal (K-12) (2 distinct):"
    assert out.splitlines()[1] == "10 (2)"


def test_column_values_rejects_unknown_column(database):
    assert database.column_values("regions", "nope").startswith("ERROR: unknown column 'nope'")


def test_execute_cannot_write_files_via_vacuum_into_or_attach(database, tmp_path):
    for sql in (f"VACUUM INTO '{tmp_path / 'copy.db'}'", f"ATTACH DATABASE '{tmp_path / 'new.db'}' AS x"):
        with pytest.raises(QueryError, match="not authorized|authorization denied"):
            database.execute(sql)
    assert not (tmp_path / "copy.db").exists() and not (tmp_path / "new.db").exists()


def test_execute_still_allows_ctes_recursion_and_window_functions(database):
    sql = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 3) "
           "SELECT x, ROW_NUMBER() OVER (ORDER BY x DESC) FROM c")
    assert sorted(database.execute(sql)) == [(1, 3), (2, 2), (3, 1)]


def test_check_single_select_allows_one_select_or_with():
    from sqlagent.db import check_single_select
    for ok in ("SELECT 1;", "  select 1 ; ", "WITH a AS (SELECT 1) SELECT * FROM a", "/* c */ (SELECT 1)",
               "-- note\nSELECT 2"):
        check_single_select(ok)
    for bad in ("SELECT 1; DROP TABLE x", "SET TRANSACTION READ WRITE", "DELETE FROM t", "",
                "VACUUM INTO 'x'", "SELECT 1;\nSELECT 2",
                "SELECT 'a;b'"):  # conservative: a ';' inside a literal is rejected too; the model can rewrite
        with pytest.raises(QueryError):
            check_single_select(bad)


def test_plain_makes_driver_types_readable():
    from datetime import date, datetime
    from decimal import Decimal

    from sqlagent.db import _plain
    assert _plain(Decimal("2.50")) == 2.5 and _plain(Decimal("10")) == 10
    assert _plain(date(2020, 1, 2)) == "2020-01-02" and _plain(datetime(2020, 1, 2, 3, 4)) == "2020-01-02T03:04:00"
    assert _plain(b"\xffok") == "�ok" and _plain("x") == "x"


def test_urls_always_name_their_driver():
    from sqlagent.db import _normalize
    assert _normalize("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _normalize("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _normalize("mysql://u:p@h/db") == "mysql+pymysql://u:p@h/db"
    assert _normalize("postgresql+psycopg2://u@h/db") == "postgresql+psycopg2://u@h/db"
    assert _normalize("sqlite:///x.db") == "sqlite:///x.db"
