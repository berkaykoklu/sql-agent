import json
import shutil
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import sqlagent.db
from sqlagent.web import create_app


def reply(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))],
              usage=NS(prompt_tokens=100, completion_tokens=10))


def call(name, args, id="c1"):
    return NS(id=id, function=NS(name=name, arguments=json.dumps(args)))


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def bird_dir(db, tmp_path, monkeypatch):
    root = tmp_path / "dbs"
    (root / "shop").mkdir(parents=True)
    shutil.copy(db, root / "shop" / "shop.sqlite")
    shutil.copytree(db.parent / "database_description", root / "shop" / "database_description")
    monkeypatch.setattr(sqlagent.db, "DB_DIR", root)
    monkeypatch.delenv("PG_URL", raising=False)
    return root


def events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def app_with(replies):
    return TestClient(create_app(lambda: FakeClient(replies)), base_url="http://127.0.0.1")


def test_sources_list_bird_databases_and_no_postgres_without_pg_url(bird_dir):
    sources = app_with([]).get("/api/sources").json()["sources"]
    assert {"id": "sqlite:shop", "label": "shop · SQLite"} in sources
    assert not any(s["id"].startswith("postgres:") for s in sources)


def test_connect_returns_the_schema_map(bird_dir):
    r = app_with([]).post("/api/connect", json={"source": "sqlite:shop"})
    assert r.status_code == 200
    schema = r.json()["schema"]
    assert {"name": "items", "rows": 6} in schema["tables"] and schema["joins"]


def test_connect_errors_are_400_with_a_message(bird_dir):
    client = app_with([])
    assert client.post("/api/connect", json={"source": "sqlite:nope"}).status_code == 400
    bad = client.post("/api/connect", json={"url": "postgresql+psycopg://x:y@127.0.0.1:1/none"})
    assert bad.status_code == 400 and bad.json()["detail"]


def test_ask_streams_steps_then_the_final_answer(bird_dir):
    client = app_with([
        reply(tool_calls=[call("describe_table", {"table": "items"})]),
        reply("<sql>SELECT name FROM items WHERE id = 1</sql>"),
    ])
    session = client.post("/api/connect", json={"source": "sqlite:shop"}).json()["session"]
    got = events(client.get("/api/ask", params={"session": session, "q": "Which fruit has id 1?"}).text)
    assert [kind for kind, _ in got] == ["step", "final"]
    assert got[0][1]["tool"] == "describe_table"
    final = got[1][1]
    assert final["sql"] == "SELECT name FROM items WHERE id = 1"
    assert final["columns"] == ["name"] and final["rows"] == [["apple"]] and final["steps"] == 1


def test_model_failure_ends_the_stream_with_an_error_event(bird_dir):
    client = app_with([RuntimeError("rate limited")])
    session = client.post("/api/connect", json={"source": "sqlite:shop"}).json()["session"]
    got = events(client.get("/api/ask", params={"session": session, "q": "q"}).text)
    assert got == [("error", {"message": "rate limited"})]


def test_unknown_session_is_404(bird_dir):
    assert app_with([]).get("/api/ask", params={"session": "nope", "q": "q"}).status_code == 404


def test_connect_with_a_table_selection_scopes_the_schema(bird_dir):
    r = app_with([]).post("/api/connect", json={"source": "sqlite:shop", "tables": ["items", "regions"]})
    assert [t["name"] for t in r.json()["schema"]["tables"]] == ["items", "regions"]


def test_requests_for_other_host_names_are_refused(bird_dir):
    # DNS rebinding: a web page pointing its own domain at 127.0.0.1 must not reach the API
    r = app_with([]).get("/api/sources", headers={"host": "evil.example"})
    assert r.status_code == 400


def test_sql_highlighting_keeps_literals_readable_and_escaped():
    import re
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    page = Path("sqlagent/web/index.html").read_text()
    code = re.search(r"// --- highlight\n(.*?)// --- end highlight", page, re.S).group(1)
    out = subprocess.run([node, "-e", code + "console.log(highlight(process.argv[1]))", "--",
                          "SELECT a FROM t WHERE x = 'east Bohemia' AND y = '<b>' LIMIT 3"],
                         capture_output=True, text=True, check=True).stdout
    assert "<span class=\"v\">'east Bohemia'</span>" in out and "&#39;" not in out
    assert "&lt;b&gt;" in out and "<b>" not in out.replace('<span class="v">', "")
