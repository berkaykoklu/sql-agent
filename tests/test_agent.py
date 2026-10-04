import json
from types import SimpleNamespace as NS

from sqlagent import prices
from sqlagent.agent import agent, extract_sql


def reply(content=None, tool_calls=None, prompt=100, completion=10):
    message = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(message=message)], usage=NS(prompt_tokens=prompt, completion_tokens=completion))


def call(name, args, id="c1", raw=None):
    return NS(id=id, function=NS(name=name, arguments=raw if raw is not None else json.dumps(args)))


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


def test_extract_sql_prefers_sql_tags():
    assert extract_sql("Done.\n<sql>\nSELECT 1\n</sql>") == "SELECT 1"
    assert extract_sql("```sql\nSELECT 2\n```") == "SELECT 2"
    assert extract_sql("  SELECT 3  ") == "SELECT 3"


def test_explorer_routes_each_tool_and_returns_final(database):
    final = "SELECT name FROM regions WHERE name = 'east Bohemia'"
    client = FakeClient([
        reply(tool_calls=[call("describe_table", {"table": "regions"}, "a"),
                          call("column_values", {"table": "regions", "column": "name", "search": "bohemia"}, "b")]),
        reply(tool_calls=[call("run_sql", {"query": final}, "c")]),
        reply(f"<sql>{final}</sql>"),
    ])
    r = agent(client, "q", "", database)
    assert [s["tool"] for s in r.steps] == ["describe_table", "column_values", "run_sql"]
    assert "'east Bohemia' (2)" in r.steps[1]["output"]
    assert r.sql == final and not r.exhausted
    assert [m["tool_call_id"] for m in client.calls[1]["messages"] if m["role"] == "tool"] == ["a", "b"]
    assert {t["function"]["name"] for t in client.calls[0]["tools"]} == {"describe_table", "column_values", "run_sql"}
    assert '<database name="shop"' in client.calls[0]["messages"][1]["content"]


def test_unknown_tool_and_bad_arguments_come_back_as_errors(database):
    client = FakeClient([
        reply(tool_calls=[call("drop_table", {"table": "items"}, "a"), call("run_sql", None, "b", raw="not json")]),
        reply("<sql>SELECT 1</sql>"),
    ])
    r = agent(client, "q", "", database)
    outputs = [m["content"] for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert outputs[0].startswith("ERROR: unknown tool 'drop_table'")
    assert outputs[1].startswith("ERROR: bad arguments")
    assert r.sql == "SELECT 1"


def _cap_replies(final):
    replies = [reply(tool_calls=[call("run_sql", {"query": f"SELECT {i}"})]) for i in range(14)]
    replies += [reply(tool_calls=[call("describe_table", {"table": "nope"})]),
                reply(tool_calls=[call("run_sql", {"query": "SELECT 99"})]),
                reply(final)]
    return replies


def test_step_cap_asks_for_a_final_answer_without_tools(database):
    client = FakeClient(_cap_replies("<sql>SELECT 42</sql>"))
    r = agent(client, "q", "", database)
    assert r.exhausted and len(r.steps) == 15 and len(client.calls) == 17
    assert client.calls[-1]["tool_choice"] == "none"
    assert client.calls[-1]["messages"][-1]["role"] == "user"
    assert r.sql == "SELECT 42"


def test_step_cap_falls_back_to_last_run_sql_when_final_turn_is_empty(database):
    client = FakeClient(_cap_replies(""))
    r = agent(client, "q", "", database)
    assert r.exhausted and r.sql == "SELECT 13"


def test_extract_sql_unescapes_backslash_quotes_inside_sql_tags():
    assert extract_sql('<sql>SELECT f.\\"District Code\\" FROM frpm AS f</sql>') == 'SELECT f."District Code" FROM frpm AS f'


def test_usage_and_cost_accumulate(database):
    client = FakeClient([reply(tool_calls=[call("run_sql", {"query": "SELECT 1"})]), reply("<sql>SELECT 1</sql>")])
    r = agent(client, "q", "", database)
    assert (r.input_tokens, r.output_tokens) == (200, 20) and r.cost == prices.cost(200, 20)
    assert client.calls[0]["model"] == prices.MODEL


def test_extract_sql_unescapes_xml_entities_inside_sql_tags():
    assert extract_sql("<sql>SELECT a FROM t WHERE b &gt; 1 AND c &lt;&gt; 'x &amp; y'</sql>") == \
        "SELECT a FROM t WHERE b > 1 AND c <> 'x & y'"


def test_system_prompt_names_the_dialect_and_has_no_sqlite_only_functions(database):
    client = FakeClient([reply("<sql>SELECT 1</sql>")])
    agent(client, "q", "", database)
    system = client.calls[0]["messages"][0]["content"]
    assert "expert SQLite analyst" in system and "strftime" not in system
