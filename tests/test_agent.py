import json
from types import SimpleNamespace as NS

from sqlagent import prices
from sqlagent.agent import agent, extract_sql, one_shot


def reply(content=None, tool_calls=None, prompt=100, completion=10):
    message = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(message=message)], usage=NS(prompt_tokens=prompt, completion_tokens=completion))


def call(query, id="c1", raw=None):
    arguments = raw if raw is not None else json.dumps({"query": query})
    return NS(id=id, function=NS(name="run_sql", arguments=arguments))


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


def test_extract_sql_handles_fences_and_plain_text():
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("Here you go:\n```SQL\nSELECT 2;\n```\nDone.") == "SELECT 2;"
    assert extract_sql("  SELECT 3  ") == "SELECT 3"


def test_one_shot_returns_sql_and_cost_without_tools():
    client = FakeClient([reply("```sql\nSELECT 1\n```")])
    r = one_shot(client, "q", "hint", "schema")
    assert r.sql == "SELECT 1" and r.steps == [] and not r.exhausted
    assert r.cost == prices.cost(100, 10)
    assert "tools" not in client.calls[0]
    assert client.calls[0]["model"] == prices.MODEL


def test_both_conditions_see_identical_messages(db):
    a, b = FakeClient([reply("SELECT 1")]), FakeClient([reply("SELECT 1")])
    one_shot(a, "q", "hint", "schema")
    agent(b, "q", "hint", "schema", db)
    assert a.calls[0]["messages"] == b.calls[0]["messages"]


def test_agent_feeds_error_back_and_fixes(db):
    client = FakeClient([
        reply(tool_calls=[call("SELECT nope FROM items")]),
        reply(tool_calls=[call("SELECT id FROM items WHERE id = 1")]),
        reply("```sql\nSELECT id FROM items WHERE id = 1\n```"),
    ])
    r = agent(client, "q", "", "schema", db)
    assert r.sql == "SELECT id FROM items WHERE id = 1"
    assert [s["sql"] for s in r.steps] == ["SELECT nope FROM items", "SELECT id FROM items WHERE id = 1"]
    assert r.steps[0]["output"].startswith("ERROR: no such column")
    assert client.calls[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "c1", "content": r.steps[0]["output"]
    }
    assert (r.input_tokens, r.output_tokens) == (300, 30) and not r.exhausted


def test_agent_stops_at_step_cap_and_uses_last_query(db):
    client = FakeClient([reply(tool_calls=[call(f"SELECT {i}")]) for i in range(6)])
    r = agent(client, "q", "", "schema", db, max_steps=5)
    assert r.exhausted and len(r.steps) == 5 and len(client.calls) == 6
    assert r.sql == "SELECT 4"


def test_agent_survives_malformed_tool_arguments(db):
    client = FakeClient([reply(tool_calls=[call(None, raw="not json")]), reply("SELECT 1")])
    r = agent(client, "q", "", "schema", db)
    assert r.sql == "SELECT 1" and r.steps == []
    assert client.calls[1]["messages"][-1]["content"].startswith("ERROR: arguments")
