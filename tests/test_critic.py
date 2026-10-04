import json
from types import SimpleNamespace as NS

from sqlagent.agent import agent
from sqlagent.critic import parse_review

QUESTION = "Which fruit has id 1? Give only its name."


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


def revise(requirement="Give only its name"):
    return (f"<verdict>REVISE</verdict><requirement>{requirement}</requirement>"
            "<problem>The note column is not asked for.</problem><fix>Drop the note column.</fix>")


def test_parse_review_verdicts():
    assert parse_review("<verdict>OK</verdict>", QUESTION, "") == ("OK", "")
    assert parse_review("<verdict>UNSURE</verdict>", QUESTION, "") == ("UNSURE", "")
    verdict, feedback = parse_review(revise(), QUESTION, "")
    assert verdict == "REVISE" and "Give only its name" in feedback and "Drop the note column." in feedback


def test_parse_review_rejects_ungrounded_or_malformed_reviews():
    assert parse_review(revise("Return the colour too"), QUESTION, "")[0] == "INVALID"  # quote not in question
    assert parse_review(revise("id = 1"), "q", "fruit with id = 1")[0] == "REVISE"     # quote from the hint is fine
    assert parse_review("<verdict>REVISE</verdict><problem>looks wrong</problem>", QUESTION, "")[0] == "INVALID"
    assert parse_review("<verdict>OK</verdict> or <verdict>REVISE</verdict>", QUESTION, "")[0] == "INVALID"
    assert parse_review("looks fine to me", QUESTION, "")[0] == "INVALID"


def test_grounded_feedback_goes_back_to_the_agent_and_is_rechecked(database):
    client = FakeClient([
        reply(tool_calls=[call("describe_table", {"table": "items"})]),
        reply("<sql>SELECT name, note FROM items WHERE id = 1</sql>"),
        reply(revise()),
        reply("<sql>SELECT name FROM items WHERE id = 1</sql>"),
        reply("<verdict>OK</verdict>"),
    ])
    r = agent(client, QUESTION, "hint", database, critic_rounds=2)
    assert r.sql_before_critic == "SELECT name, note FROM items WHERE id = 1"
    assert r.sql == "SELECT name FROM items WHERE id = 1"
    assert [c["verdict"] for c in r.critic] == ["REVISE", "OK"] and not r.reverted
    critic_prompt = client.calls[2]["messages"][1]["content"]
    assert "table items:" in critic_prompt and "('apple', 'red')" in critic_prompt and '<database name="shop"' in critic_prompt
    assert "tools" not in client.calls[2]
    to_agent = client.calls[3]["messages"][-1]["content"]
    assert "Drop the note column." in to_agent and "run_sql" in to_agent
    assert r.input_tokens == 500


def test_failing_sql_goes_back_to_the_agent_without_asking_the_critic(database):
    client = FakeClient([
        reply("<sql>SELECT nope FROM items</sql>"),
        reply("<sql>SELECT name FROM items WHERE id = 1</sql>"),
        reply("<verdict>OK</verdict>"),
    ])
    r = agent(client, QUESTION, "", database, critic_rounds=2)
    assert [c["verdict"] for c in r.critic] == ["ERROR", "OK"]
    assert "no such column" in client.calls[1]["messages"][-1]["content"]
    assert r.sql == "SELECT name FROM items WHERE id = 1"


def test_invalid_or_unsure_reviews_change_nothing(database):
    for review in (revise("Return the colour too"), "<verdict>UNSURE</verdict>"):
        client = FakeClient([reply("<sql>SELECT name FROM items WHERE id = 1</sql>"), reply(review)])
        r = agent(client, QUESTION, "", database, critic_rounds=2)
        assert r.sql == r.sql_before_critic and len(client.calls) == 2 and r.critic[0]["verdict"] in ("INVALID", "UNSURE")


def test_revision_that_loses_all_rows_is_reverted(database):
    client = FakeClient([
        reply("<sql>SELECT name FROM items WHERE id = 1</sql>"),
        reply(revise()),
        reply("<sql>SELECT name FROM items WHERE id = 99</sql>"),
    ])
    r = agent(client, QUESTION, "", database, critic_rounds=2)
    assert r.reverted and r.sql == "SELECT name FROM items WHERE id = 1" and len(client.calls) == 3


def test_without_critic_rounds_nothing_is_recorded(database):
    r = agent(FakeClient([reply("<sql>SELECT 1</sql>")]), "q", "", database)
    assert r.sql_before_critic is None and r.critic == []
