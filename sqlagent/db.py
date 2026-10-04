import csv
import io
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "data/minidev/MINIDEV/dev_databases"
QUESTIONS = ROOT / "data/minidev/MINIDEV/mini_dev_sqlite.json"
MAX_ROWS = 20
MAX_LINE = 300
# mode=ro stops writes to the database, but VACUUM INTO and ATTACH can still create files elsewhere
_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


class QueryError(Exception):
    pass


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


def execute(db_path: Path | str, sql: str, timeout: float = 10.0, params: tuple = ()) -> list[tuple]:
    conn = _connect(db_path)
    deadline = time.monotonic() + timeout
    # sqlite3's own timeout only covers lock waits; this aborts a running query
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        raise QueryError("timeout" if time.monotonic() > deadline else str(e)) from e
    finally:
        conn.close()


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LINE else text[:MAX_LINE] + "..."


def _line(row: tuple) -> str:
    return _clip(repr(row))


def run(db_path: Path | str, sql: str, timeout: float = 10.0) -> str:
    try:
        rows = execute(db_path, sql, timeout)
    except QueryError as e:
        return f"ERROR: {e}"
    if not rows:
        return "(0 rows)"
    text = "\n".join(_line(r) for r in rows[:MAX_ROWS])
    if len(rows) > MAX_ROWS:
        text += f"\n... ({len(rows)} rows total)"
    return text


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r[0] for r in rows]


def _resolve(db_path: Path | str, table: str, column: str | None = None) -> tuple[str, str | None]:
    # model-supplied identifiers are only ever interpolated after matching a real name
    conn = _connect(db_path)
    try:
        tables = {t.lower(): t for t in _tables(conn)}
        real = tables.get(str(table).lower())
        if real is None:
            raise QueryError(f"unknown table '{table}'; tables: {', '.join(tables.values())}")
        if column is None:
            return real, None
        columns = {r[1].lower(): r[1] for r in conn.execute(f"PRAGMA table_info({_quote(real)})")}
        col = columns.get(str(column).lower())
        if col is None:
            raise QueryError(f"unknown column '{column}' in {real}; columns: {', '.join(columns.values())}")
        return real, col
    finally:
        conn.close()


@lru_cache(maxsize=None)
def _descriptions(db_path: Path, table: str) -> dict[str, str]:
    folder = db_path.parent / "database_description"
    files = {p.stem.lower(): p for p in folder.glob("*.csv")} if folder.exists() else {}
    path = files.get(table.lower())  # BIRD file names don't always match the table's case
    if path is None:
        return {}
    text = path.read_bytes().decode("utf-8-sig", errors="replace")  # 4 BIRD files are not UTF-8
    notes = {}
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("original_column_name") or "").strip()
        parts: list[str] = []
        for key in ("column_name", "column_description"):
            value = " ".join((row.get(key) or "").split())
            if value and value.lower() != name.lower() and value not in parts:
                parts.append(value)
        values = " ".join((row.get("value_description") or "").split())
        if values:
            parts.append(f"values: {values}")
        if name and parts:
            notes[name.lower()] = "; ".join(parts)
    return notes


def overview(db_path: Path | str) -> str:
    conn = _connect(db_path)
    try:
        tables = _tables(conn)
        counts = [conn.execute(f"SELECT COUNT(*) FROM {_quote(t)}").fetchone()[0] for t in tables]
        joins = [
            f"{t}.{fk[3]} → {fk[2]}.{fk[4]}" if fk[4] else f"{t}.{fk[3]} → {fk[2]}"
            for t in tables
            for fk in conn.execute(f"PRAGMA foreign_key_list({_quote(t)})")
        ]
    finally:
        conn.close()
    listing = "\n".join(f"{t} ({n:,} rows)" for t, n in zip(tables, counts))
    return (f'<database name="{Path(db_path).stem}">\n<tables>\n{listing}\n</tables>\n'
            f"<joins>\n{chr(10).join(joins) or '(none declared)'}\n</joins>\n</database>")


def describe_table(db_path: Path | str, table: str) -> str:
    try:
        real, _ = _resolve(db_path, table)
    except QueryError as e:
        return f"ERROR: {e}"
    conn = _connect(db_path)
    try:
        columns = conn.execute(f"PRAGMA table_info({_quote(real)})").fetchall()
        fks = {fk[3]: f"{fk[2]}.{fk[4]}" if fk[4] else fk[2]
               for fk in conn.execute(f"PRAGMA foreign_key_list({_quote(real)})")}
    finally:
        conn.close()
    notes = _descriptions(Path(db_path).resolve(), real)
    lines = [f"table {real}:"]
    for _, name, type_, _, _, pk in columns:
        line = f"- {name} {type_ or 'ANY'}"
        if pk:
            line += " PRIMARY KEY"
        if name in fks:
            line += f" → {fks[name]}"
        if name.lower() in notes:
            line += f" — {notes[name.lower()]}"
        lines.append(_clip(line))
    return "\n".join(lines)


def column_values(db_path: Path | str, table: str, column: str, search: str | None = None) -> str:
    try:
        real, col = _resolve(db_path, table, column)
        t, c = _quote(real), _quote(col)
        if search:
            literal = str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = execute(
                db_path,
                f"SELECT {c}, COUNT(*) FROM {t} WHERE {c} LIKE ? ESCAPE '\\' "
                f"GROUP BY {c} ORDER BY 2 DESC LIMIT 20",
                params=(f"%{literal}%",),
            )
            header = f"values of {real}.{col} matching '{search}':"
        else:
            rows = execute(db_path, f"SELECT {c}, COUNT(*) FROM {t} GROUP BY {c} ORDER BY 2 DESC LIMIT 10")
            distinct = execute(db_path, f"SELECT COUNT(DISTINCT {c}) FROM {t}")[0][0]
            header = f"most common values of {real}.{col} ({distinct} distinct):"
    except QueryError as e:
        return f"ERROR: {e}"
    if not rows:
        return f"{header}\n(no matching values)"
    return header + "\n" + "\n".join(_clip(f"{value!r} ({count})") for value, count in rows)


def same_result(pred: list[tuple], gold: list[tuple]) -> bool:
    return set(pred) == set(gold)  # official BIRD EX: ignores order and duplicates
