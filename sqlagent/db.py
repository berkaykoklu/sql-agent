import re
import sqlite3
import time
import warnings
from contextlib import contextmanager
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import bindparam, column, create_engine, distinct, event, func, inspect, select, table
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from sqlagent import catalog as cat

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "data/minidev/MINIDEV/dev_databases"
QUESTIONS = ROOT / "data/minidev/MINIDEV/mini_dev_sqlite.json"
MAX_ROWS = 20
MAX_LINE = 300
DIALECTS = {"sqlite": "SQLite", "postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}
# mode=ro stops writes to the database, but VACUUM INTO and ATTACH can still create files elsewhere
_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


class QueryError(Exception):
    pass


_LEADING = re.compile(r"^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*\(*\s*(SELECT|WITH)\b", re.I | re.S)


def check_single_select(sql: str) -> None:
    """Server dialects run multi-statement strings (psycopg does), so only one SELECT/WITH may reach them."""
    body = sql.strip().rstrip(";").rstrip()
    if ";" in body:
        raise QueryError("only one statement is allowed")
    if not _LEADING.match(body):
        raise QueryError("only SELECT or WITH queries are allowed")


def _normalize(url: str) -> str:
    # always name the driver: sources disagree on the default for postgresql://
    for plain, full in (("postgresql://", "postgresql+psycopg://"), ("postgres://", "postgresql+psycopg://"),
                        ("mysql://", "mysql+pymysql://")):
        if url.startswith(plain):
            return full + url[len(plain):]
    return url


def db_file(db_id: str) -> Path:
    path = DB_DIR / db_id / f"{db_id}.sqlite"
    if not path.exists():
        known = sorted(p.name for p in DB_DIR.iterdir())
        raise SystemExit(f"unknown db '{db_id}'; choose from: {', '.join(known)}")
    return path


def _connect(db_path: Path | str) -> sqlite3.Connection:
    # as_uri() percent-encodes the space in "My Road"; mode=ro makes writes impossible
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode(errors="replace")  # some BIRD text is not UTF-8
    return conn


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LINE else text[:MAX_LINE] + "..."


def _plain(value):
    """Readable form for tool output; scoring keeps the raw values."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime, dtime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode(errors="replace")
    return value


def _line(row: tuple) -> str:
    return _clip(repr(tuple(_plain(v) for v in row)))


def _type_name(col: dict) -> str:
    try:
        return str(col["type"]) or "ANY"
    except Exception:  # NullType (no declared type) cannot be rendered
        return "ANY"


class Database:
    def __init__(self, url: str | None, catalog: cat.Catalog | None = None, tables: list[str] | None = None,
                 timeout: float = 10.0, *, _creator=None, _name: str | None = None):
        if _creator is not None:
            self.engine = create_engine("sqlite://", creator=_creator, poolclass=NullPool)
        else:
            self.engine = create_engine(_normalize(url), pool_pre_ping=True)
        self.kind = self.engine.dialect.name
        if self.kind in ("mysql", "mariadb"):
            event.listen(self.engine, "connect", self._mysql_session)
        elif self.kind not in ("sqlite", "postgresql"):
            warnings.warn(f"{self.kind}: no read-only guard; safety relies on the database user's grants")
        self.dialect = DIALECTS.get(self.kind, self.kind)
        self.name = _name or self.engine.url.database or self.kind
        self.catalog = catalog
        self.timeout = timeout

        # reflect once: tool calls never touch the Inspector (thread-safe for parallel eval workers)
        insp = inspect(self.engine)
        names = insp.get_table_names()
        if tables is not None:
            wanted = {t.lower() for t in tables}
            names = [n for n in names if n.lower() in wanted]
        self.table_names = sorted(names, key=str.lower)
        self._scope = {t.lower() for t in self.table_names} if tables is not None else None
        self._columns = {t: cols for (_, t), cols in insp.get_multi_columns(filter_names=self.table_names).items()}
        self._pks = {t: set(pk.get("constrained_columns") or [])
                     for (_, t), pk in insp.get_multi_pk_constraint(filter_names=self.table_names).items()}
        self._fks = {t: fks for (_, t), fks in insp.get_multi_foreign_keys(filter_names=self.table_names).items()}
        self._map: dict | None = None

    @classmethod
    def sqlite(cls, path: Path | str, catalog: cat.Catalog | None = None, tables: list[str] | None = None,
               timeout: float = 10.0) -> "Database":
        path = Path(path)
        return cls(None, catalog, tables, timeout, _creator=lambda: _connect(path), _name=path.stem)

    # --- execution -------------------------------------------------------------------------------

    def _mysql_session(self, dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        if "mariadb" in str(dbapi_conn.get_server_info()).lower():
            cur.execute(f"SET SESSION max_statement_time = {self.timeout}")  # seconds on MariaDB
        else:
            cur.execute(f"SET SESSION max_execution_time = {int(self.timeout * 1000)}")
        cur.close()

    @contextmanager
    def _guard(self, conn, deadline: float):
        if self.kind != "sqlite":
            yield
            return
        raw = conn.connection.driver_connection
        # sqlite3's own timeout only covers lock waits; the progress handler aborts a running query
        raw.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        raw.set_authorizer(self._authorize)
        try:
            yield
        finally:  # the Inspector needs PRAGMA, so the guard only lives around one query
            raw.set_progress_handler(None, 0)
            raw.set_authorizer(None)

    def _query(self, statement) -> tuple[list[str], list[tuple]]:
        if isinstance(statement, str) and self.kind != "sqlite":
            check_single_select(statement)  # SQLite has the authorizer and its driver runs one statement only
        if isinstance(statement, str) and self.engine.dialect.paramstyle in ("format", "pyformat"):
            statement = statement.replace("%", "%%")  # psycopg/pymysql read a bare % in LIKE '%x%' as a placeholder
        deadline = time.monotonic() + self.timeout
        try:
            with self.engine.connect() as conn:
                if self.kind == "postgresql":
                    conn = conn.execution_options(postgresql_readonly=True)
                with conn.begin(), self._guard(conn, deadline):
                    if self.kind == "postgresql":
                        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(self.timeout * 1000)}")
                    # model SQL goes to the driver untouched: text() would read ':23' in '1:23' as a parameter
                    result = conn.exec_driver_sql(statement) if isinstance(statement, str) else conn.execute(statement)
                    return list(result.keys()), [tuple(r) for r in result.all()]
        except SQLAlchemyError as e:
            message = str(getattr(e, "orig", None) or e)
            if time.monotonic() > deadline or "statement timeout" in message or "execution time exceeded" in message:
                raise QueryError("timeout") from e
            raise QueryError(message.strip()) from e

    def _authorize(self, action, arg1, *_):
        if action not in _READ_ACTIONS:
            return sqlite3.SQLITE_DENY
        # with a table scope, model SQL may not read other tables (or sqlite_master) either
        if action == sqlite3.SQLITE_READ and self._scope is not None and str(arg1).lower() not in self._scope:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _rows(self, statement) -> list[tuple]:
        return self._query(statement)[1]

    def execute(self, sql: str) -> list[tuple]:
        return self._rows(sql)

    def fetch(self, sql: str, limit: int = 50) -> tuple[list[str], list[list]]:
        """Column names and the first rows, JSON-ready, for the result table in the web UI."""
        columns, rows = self._query(sql)
        return columns, [[_plain(v) for v in r] for r in rows[:limit]]

    def run(self, sql: str) -> str:
        try:
            rows = self.execute(sql)
        except QueryError as e:
            return f"ERROR: {e}"
        if not rows:
            return "(0 rows)"
        text = "\n".join(_line(r) for r in rows[:MAX_ROWS])
        if len(rows) > MAX_ROWS:
            text += f"\n... ({len(rows)} rows total)"
        return text

    # --- what the agent can look at --------------------------------------------------------------

    def _resolve(self, table_name: str, column_name: str | None = None) -> tuple[str, str | None]:
        # model-supplied identifiers are only ever used after matching a reflected name
        tables = {t.lower(): t for t in self.table_names}
        real = tables.get(str(table_name).lower())
        if real is None:
            raise QueryError(f"unknown table '{table_name}'; tables: {', '.join(self.table_names)}")
        if column_name is None:
            return real, None
        columns = {c["name"].lower(): c["name"] for c in self._columns[real]}
        col = columns.get(str(column_name).lower())
        if col is None:
            raise QueryError(f"unknown column '{column_name}' in {real}; columns: {', '.join(columns.values())}")
        return real, col

    def schema_map(self) -> dict:
        """Tables with row counts and joins as (table, column) pairs: feeds the overview text and the UI graph."""
        if self._map is None:
            joins = [{"from": [t, src], "to": [fk["referred_table"], dst]}
                     for t in self.table_names for fk in self._fks.get(t, [])
                     for src, dst in zip(fk["constrained_columns"], fk["referred_columns"] or [None] * 99)]
            self._map = {"name": self.name, "dialect": self.dialect, "joins": joins,
                         "tables": [{"name": t, "rows": self._count(t)} for t in self.table_names]}
        return self._map

    def _count(self, table_name: str) -> int | None:
        try:  # a huge table can exceed the query timeout; the map must still load
            return self._rows(select(func.count()).select_from(table(table_name)))[0][0]
        except QueryError:
            return None

    def overview(self) -> str:
        m = self.schema_map()
        listing = "\n".join(f"{t['name']} ({'?' if t['rows'] is None else format(t['rows'], ',')} rows)" for t in m["tables"])
        joins = [f"{j['from'][0]}.{j['from'][1]} → {j['to'][0]}" + (f".{j['to'][1]}" if j["to"][1] else "")
                 for j in m["joins"]]
        return (f'<database name="{m["name"]}" dialect="{m["dialect"]}">\n<tables>\n{listing}\n</tables>\n'
                f"<joins>\n{chr(10).join(joins) or '(none declared)'}\n</joins>\n</database>")

    def describe_table(self, table_name: str) -> str:
        try:
            real, _ = self._resolve(table_name)
        except QueryError as e:
            return f"ERROR: {e}"
        fks = {src: f"{fk['referred_table']}.{dst}" if dst else fk["referred_table"]
               for fk in self._fks.get(real, [])
               for src, dst in zip(fk["constrained_columns"], fk["referred_columns"] or [None] * 99)}
        lines = [f"table {real}:"]
        if note := cat.table_note(self.catalog, real):
            lines.append(f"  {note}")
        for col in self._columns[real]:
            name = col["name"]
            line = f"- {name} {_type_name(col)}"
            if name in self._pks.get(real, set()):
                line += " PRIMARY KEY"
            if name in fks:
                line += f" → {fks[name]}"
            if note := cat.column_note(self.catalog, real, name) or (col.get("comment") or ""):
                line += f" — {note}"
            lines.append(_clip(line))
        return "\n".join(lines)

    def column_values(self, table_name: str, column_name: str, search: str | None = None) -> str:
        try:
            real, col = self._resolve(table_name, column_name)
            c = table(real, column(col)).c[col]
            if search:
                literal = str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = self._rows(select(c, func.count())
                                  .where(c.ilike(bindparam("p", f"%{literal}%"), escape="\\"))
                                  .group_by(c).order_by(func.count().desc()).limit(20))
                header = f"values of {real}.{col} matching '{search}':"
            else:
                rows = self._rows(select(c, func.count()).group_by(c).order_by(func.count().desc()).limit(10))
                n = self._rows(select(func.count(distinct(c))).select_from(c.table))[0][0]
                header = f"most common values of {real}.{col} ({n} distinct):"
        except QueryError as e:
            return f"ERROR: {e}"
        if not rows:
            return f"{header}\n(no matching values)"
        return header + "\n" + "\n".join(_clip(f"{_plain(value)!r} ({count})") for value, count in rows)


def bird_sqlite(db_id: str, tables: list[str] | None = None, timeout: float = 10.0) -> Database:
    path = db_file(db_id)
    return Database.sqlite(path, catalog=cat.from_bird_csv(path.parent / "database_description"),
                           tables=tables, timeout=timeout)


def bird(db_id: str, url: str | None = None, timeout: float = 10.0) -> Database:
    """A BIRD database: the local SQLite copy, or the same tables on a server (e.g. the PostgreSQL dump)."""
    sqlite = bird_sqlite(db_id, timeout=timeout)
    if url is None:
        return sqlite
    return Database(url, catalog=sqlite.catalog, tables=sqlite.table_names, timeout=timeout, _name=db_id)


def same_result(pred: list[tuple], gold: list[tuple]) -> bool:
    return set(pred) == set(gold)  # official BIRD EX: ignores order and duplicates
