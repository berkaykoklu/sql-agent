import json
from types import SimpleNamespace as NS

from eval.run import evaluate, load_done, pilot_sample


def reply(content):
    return NS(choices=[NS(message=NS(content=content, tool_calls=None))],
              usage=NS(prompt_tokens=100, completion_tokens=10))


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def question(gold):
    return {"question_id": 7, "db_id": "shop", "difficulty": "simple",
            "question": "q", "evidence": "", "SQL": gold}


def test_correct_answer_is_scored_ok(db):
    row = evaluate(FakeClient([reply("SELECT name FROM items WHERE id = 1")]),
                   question("SELECT name FROM items WHERE id = 1"), "one_shot", db)
    assert row["status"] == "ok" and row["correct"] is True and row["cost"] > 0


def test_broken_prediction_is_wrong_not_fatal(db):
    row = evaluate(FakeClient([reply("SELECT nope FROM items")]),
                   question("SELECT name FROM items"), "one_shot", db)
    assert row["status"] == "ok" and row["correct"] is False


def test_empty_sql_never_matches_empty_gold(db):
    row = evaluate(FakeClient([reply("")]), question("SELECT id FROM items WHERE id > 99"), "agent", db)
    assert row["correct"] is False


def test_failing_gold_is_excluded(db):
    row = evaluate(FakeClient([]), question("SELECT nope FROM items"), "one_shot", db)
    assert row["status"] == "gold_failed"


def test_api_failure_is_recorded_as_error(db):
    class Down:
        chat = NS(completions=NS(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("503"))))
    row = evaluate(Down(), question("SELECT 1"), "one_shot", db)
    assert row["status"] == "error" and "503" in row["error"]


def test_load_done_retries_errors_and_respects_later_rows(tmp_path):
    path = tmp_path / "r.jsonl"
    rows = [
        {"question_id": 1, "condition": "agent", "status": "error"},
        {"question_id": 1, "condition": "agent", "status": "ok"},
        {"question_id": 2, "condition": "agent", "status": "ok"},
        {"question_id": 2, "condition": "agent", "status": "error"},
        {"question_id": 3, "condition": "one_shot", "status": "gold_failed"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert load_done(path) == {(1, "agent"), (3, "one_shot")}
    assert load_done(tmp_path / "missing.jsonl") == set()


def test_pilot_sample_is_stratified_and_deterministic():
    qs = [{"question_id": i, "difficulty": d}
          for i, d in enumerate(["simple"] * 30 + ["moderate"] * 50 + ["challenging"] * 20)]
    sample = pilot_sample(qs)
    assert [q["difficulty"] for q in sample].count("moderate") == 10 and len(sample) == 20
    assert sample == pilot_sample(qs)


def test_every_condition_dispatches_to_the_right_call(db):
    shapes = {}
    for condition in ("one_shot", "one_shot_repeat", "agent", "agent_verify"):
        client = FakeClient([reply("SELECT 1")])
        row = evaluate(client, question("SELECT 1"), condition, db)
        assert row["status"] == "ok" and row["condition"] == condition
        shapes[condition] = ("tools" in client.calls[0], "always run" in client.calls[0]["messages"][0]["content"])
    assert shapes == {"one_shot": (False, False), "one_shot_repeat": (False, False),
                      "agent": (True, False), "agent_verify": (True, True)}
