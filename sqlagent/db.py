import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "data/minidev/MINIDEV/dev_databases"
QUESTIONS = ROOT / "data/minidev/MINIDEV/mini_dev_sqlite.json"
MAX_ROWS = 20
MAX_LINE = 300


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


def execute(db_path: Path | str, sql: str, timeout: float = 10.0) -> list[tuple]:
    conn = _connect(db_path)
    deadline = time.monotonic() + timeout
    # sqlite3's own timeout only covers lock waits; this aborts a running query
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.Error as e:
        raise QueryError("timeout" if time.monotonic() > deadline else str(e)) from e
    finally:
        conn.close()


def _line(row: tuple) -> str:
    text = repr(row)
    return text if len(text) <= MAX_LINE else text[:MAX_LINE] + "..."


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


def schema(db_path: Path | str, sample_rows: int = 3) -> str:
    conn = _connect(db_path)
    try:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        parts = []
        for name, create in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = conn.execute(f"SELECT * FROM {quoted} LIMIT {sample_rows}").fetchall()
            sample = "\n".join(_line(r) for r in rows)
            parts.append(f"{create};\n/* {sample_rows} sample rows:\n{sample}\n*/")
        return "\n\n".join(parts)
    finally:
        conn.close()


def same_result(pred: list[tuple], gold: list[tuple]) -> bool:
    return set(pred) == set(gold)  # official BIRD EX: ignores order and duplicates
