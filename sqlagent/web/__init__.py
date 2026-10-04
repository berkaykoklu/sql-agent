"""Local web UI: connect a database, ask a question, watch the agent explore (FastAPI, 127.0.0.1 only)."""
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import sqlagent.db as dbmod
from sqlagent.agent import agent
from sqlagent.db import Database, QueryError, bird

PAGE = Path(__file__).with_name("index.html")


class Connect(BaseModel):
    source: str | None = None  # "sqlite:<bird id>" or "postgres:<bird id>"
    url: str | None = None
    catalog: dict | None = None
    tables: list[str] | None = None


def _sources() -> list[dict]:
    ids = sorted(p.name for p in dbmod.DB_DIR.iterdir() if p.is_dir()) if dbmod.DB_DIR.exists() else []
    out = [{"id": f"sqlite:{i}", "label": f"{i} · SQLite"} for i in ids]
    if os.environ.get("PG_URL"):
        out += [{"id": f"postgres:{i}", "label": f"{i} · PostgreSQL"} for i in ids]
    return out


def _open(body: Connect) -> Database:
    if body.source:
        kind, _, db_id = body.source.partition(":")
        url = os.environ.get("PG_URL") if kind == "postgres" else None
        base = bird(db_id, url)
        if not body.tables:
            return base
        # rebuild from the URL itself: str(engine.url) would mask the password as ***
        return Database(url, catalog=base.catalog, tables=body.tables) if url else dbmod.bird_sqlite(db_id, tables=body.tables)
    if body.url:
        return Database(body.url, catalog=body.catalog, tables=body.tables)
    raise ValueError("give a source or a url")


def create_app(client_factory=None) -> FastAPI:
    if client_factory is None:
        from openai import OpenAI
        client_factory = lambda: OpenAI(max_retries=5)  # noqa: E731
    app = FastAPI(title="sql-agent")
    sessions: dict[str, Database] = {}

    @app.get("/")
    def page():
        return FileResponse(PAGE)

    @app.get("/api/sources")
    def sources():
        return {"sources": _sources()}

    @app.post("/api/connect")
    def connect(body: Connect):
        try:
            db = _open(body)
            schema = db.schema_map()
        except (Exception, SystemExit) as e:  # bad URL, unreachable server, unknown BIRD id
            raise HTTPException(400, str(getattr(e, "orig", None) or e).strip()) from None
        session = uuid.uuid4().hex
        sessions[session] = db
        return {"session": session, "schema": schema}

    @app.get("/api/ask")
    def ask(session: str, q: str, evidence: str = ""):
        db = sessions.get(session)
        if db is None:
            raise HTTPException(404, "unknown session; connect again")
        events: queue.Queue = queue.Queue()

        def work():
            try:
                start = time.monotonic()
                r = agent(client_factory(), q, evidence, db, on_step=lambda s: events.put(("step", s)))
                final = {"sql": r.sql, "seconds": round(time.monotonic() - start, 2), "cost": r.cost,
                         "steps": len(r.steps), "exhausted": r.exhausted, "columns": [], "rows": []}
                try:
                    final["columns"], final["rows"] = db.fetch(r.sql) if r.sql.strip() else ([], [])
                except QueryError as e:
                    final["error"] = str(e)
                events.put(("final", final))
            except Exception as e:  # model or network failure must end the stream, not hang it
                events.put(("error", {"message": str(e)}))
            finally:
                events.put(None)

        threading.Thread(target=work, daemon=True).start()

        def stream():
            while (item := events.get()) is not None:
                kind, data = item
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def main() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
