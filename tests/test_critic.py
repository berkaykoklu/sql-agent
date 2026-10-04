import json
from types import SimpleNamespace as NS

from sqlagent.agent import agent
from sqlagent.critic import parse_review


def reply(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))],
              usage=NS(prompt_tokens=100, completion_tokens=10))


def call(name, args, id="c1"):
    return NS(id=id, function=NS(name=name, arguments=json.dumps(args)))


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


REVISE = "<verdict>REVISE</verdict><feedback>Check 2: drop the note column.</feedback>"


def test_parse_review_accepts_ok_and_revise_and_defaults_to_ok():
    assert parse_review("<verdict>OK</verdict>") == ("OK", "")
    assert parse_review(REVISE) == ("REVISE", "Check 2: drop the note column.")
    assert parse_review("looks fine to me") == ("OK", "")
    assert parse_review("<verdict>REVISE</verdict>") == ("OK", "")  # no concrete problem given


def test_critic_feedback_goes_back_to_the_agent_and_is_rechecked(db):
    client = FakeClient([
        reply(tool_calls=[call("describe_table", {"table": "items"})]),
        reply("<sql>SELECT name, note FROM items WHERE id = 1</sql>"),
        reply(REVISE),
        reply("<sql>SELECT name FROM items WHERE id = 1</sql>"),
        reply("<verdict>OK</verdict>"),
    ])
    r = agent(client, "q", "hint", "MAP", db, critic_rounds=2)
    assert r.sql_before_critic == "SELECT name, note FROM items WHERE id = 1"
    assert r.sql == "SELECT name FROM items WHERE id = 1"
    assert [c["verdict"] for c in r.critic] == ["REVISE", "OK"] and not r.reverted
    critic_prompt = client.calls[2]["messages"][1]["content"]
    assert "table items:" in critic_prompt and "('apple', 'red')" in critic_prompt and "MAP" in critic_prompt
    assert "tools" not in client.calls[2]
    assert "Check 2: drop the note column." in client.calls[3]["messages"][-1]["content"]
    assert r.input_tokens == 500


def test_critic_ok_on_first_look_changes_nothing(db):
    client = FakeClient([reply("<sql>SELECT 1</sql>"), reply("<verdict>OK</verdict>")])
    r = agent(client, "q", "", "MAP", db, critic_rounds=2)
    assert r.sql == r.sql_before_critic == "SELECT 1" and len(client.calls) == 2


def test_revision_that_loses_all_rows_is_reverted(db):
    client = FakeClient([
        reply("<sql>SELECT name FROM items WHERE id = 1</sql>"),
        reply(REVISE),
        reply("<sql>SELECT name FROM items WHERE id = 99</sql>"),
    ])
    r = agent(client, "q", "", "MAP", db, critic_rounds=2)
    assert r.reverted and r.sql == "SELECT name FROM items WHERE id = 1" and len(client.calls) == 3


def test_without_critic_rounds_nothing_is_recorded(db):
    r = agent(FakeClient([reply("<sql>SELECT 1</sql>")]), "q", "", "MAP", db)
    assert r.sql_before_critic is None and r.critic == []
