import json
from types import SimpleNamespace as NS

from eval.run import evaluate, load_done, sample_by_difficulty


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
                   question("SELECT name FROM items WHERE id = 1"), "explorer", db)
    assert row["status"] == "ok" and row["correct"] is True and row["cost"] > 0


def test_broken_prediction_is_wrong_not_fatal(db):
    row = evaluate(FakeClient([reply("SELECT nope FROM items")]),
                   question("SELECT name FROM items"), "explorer", db)
    assert row["status"] == "ok" and row["correct"] is False


def test_empty_sql_never_matches_empty_gold(db):
    row = evaluate(FakeClient([reply("")]), question("SELECT id FROM items WHERE id > 99"), "explorer", db)
    assert row["correct"] is False


def test_failing_gold_is_excluded(db):
    row = evaluate(FakeClient([]), question("SELECT nope FROM items"), "explorer", db)
    assert row["status"] == "gold_failed"


def test_api_failure_is_recorded_as_error(db):
    class Down:
        chat = NS(completions=NS(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("503"))))
    row = evaluate(Down(), question("SELECT 1"), "explorer", db)
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


def test_sample_by_difficulty_is_stratified_and_deterministic():
    qs = [{"question_id": i, "difficulty": d}
          for i, d in enumerate(["simple"] * 30 + ["moderate"] * 50 + ["challenging"] * 20)]
    sample = sample_by_difficulty(qs, 20)
    assert [q["difficulty"] for q in sample].count("moderate") == 10 and len(sample) == 20
    assert sample == sample_by_difficulty(qs, 20)
    assert len(sample_by_difficulty(qs, 60)) == 60


def test_explorer_gets_the_database_map(db):
    client = FakeClient([reply("<sql>SELECT 1</sql>")])
    row = evaluate(client, question("SELECT 1"), "explorer", db)
    assert row["status"] == "ok" and row["correct"] is True
    assert '<database name="shop">' in client.calls[0]["messages"][1]["content"]
    assert len(client.calls[0]["tools"]) == 3


def test_critic_condition_scores_the_answer_before_and_after_the_critic(db):
    client = FakeClient([
        reply("<sql>SELECT name, note FROM items WHERE id = 1</sql>"),
        reply("<verdict>REVISE</verdict><feedback>Check 2: drop note.</feedback>"),
        reply("<sql>SELECT name FROM items WHERE id = 1</sql>"),
        reply("<verdict>OK</verdict>"),
    ])
    row = evaluate(client, question("SELECT name FROM items WHERE id = 1"), "explorer_critic", db)
    assert row["correct"] is True and row["correct_before_critic"] is False
    assert [c["verdict"] for c in row["critic"]] == ["REVISE", "OK"] and row["reverted"] is False
