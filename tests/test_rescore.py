from eval.rescore import rescore


def row(qid, sql, correct, status="ok"):
    return {"question_id": qid, "db_id": "shop", "condition": "explorer", "status": status, "sql": sql, "correct": correct}


def test_rescore_rechecks_only_fixed_questions(db):
    fixes = {1: "SELECT name FROM items WHERE id = 2"}
    rows = [
        row(1, "SELECT name FROM items WHERE id = 2", False),  # matches the fixed gold → flips
        row(1, "SELECT name FROM items WHERE id = 1", True),   # matched only the old gold → flips back
        row(1, "", False),                                      # no SQL stays wrong
        row(2, "SELECT 1", True),                               # not fixed → untouched
        {"question_id": 1, "condition": "explorer", "status": "error"},
    ]
    out = rescore(rows, fixes, lambda db_id: db)
    assert [r.get("correct") for r in out] == [True, False, False, True, None]
    assert out[0]["gold_fixed"] is True and "gold_fixed" not in out[3]
