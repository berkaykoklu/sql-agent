from eval.report import summarize, svg


def row(qid, cond, correct, difficulty="simple", status="ok"):
    return {"question_id": qid, "condition": cond, "difficulty": difficulty, "status": status,
            "correct": correct, "cost": 0.001, "steps": [{}] if cond == "agent" else [], "exhausted": False}


def rows():
    out = []
    table = [  # qid, one_shot, one_shot_repeat, agent, agent_verify, difficulty
        (1, 1, 1, 1, 1, "simple"), (2, 0, 0, 1, 1, "moderate"),
        (3, 0, 1, 0, 1, "moderate"), (4, 1, 1, 1, 1, "challenging"),
    ]
    for qid, a, r, b, v, d in table:
        out += [row(qid, "one_shot", bool(a), d), row(qid, "one_shot_repeat", bool(r), d),
                row(qid, "agent", bool(b), d), row(qid, "agent_verify", bool(v), d)]
    out.insert(0, row(2, "agent", False, "moderate", status="error"))  # superseded by the later ok row
    out.append({"question_id": 5, "condition": "one_shot", "difficulty": "simple", "status": "gold_failed"})
    return out


def test_summarize_pairs_latest_rows_and_computes_delta():
    s = summarize(rows())
    assert s["n"] == 4 and s["gold_failed"] == 1 and s["errors"] == 0
    assert s["one_shot"]["acc"]["overall"] == 0.5 and s["agent"]["acc"]["overall"] == 0.75
    assert s["agent"]["acc"]["moderate"] == 0.5
    assert s["one_shot_repeat"]["delta"] == 0.25 and s["agent_verify"]["delta"] == 0.5
    low, high = s["agent"]["ci"]
    assert s["agent"]["delta"] == 0.25 and 0 <= low <= 0.25 <= high <= 1
    assert "delta" not in s["one_shot"]
    assert s["agent"]["steps"] == 1 and s["one_shot"]["steps"] == 0


def test_svg_shows_accuracies():
    out = svg(summarize(rows()))
    assert out.startswith("<svg") and "75%" in out and "100%" in out and "agent_verify" in out


def test_tool_usage_is_averaged_and_delta_needs_a_baseline():
    rs = [{"question_id": i, "condition": "explorer", "difficulty": "simple", "status": "ok",
           "correct": True, "cost": 0.001, "exhausted": False,
           "steps": [{"tool": "describe_table"}, {"tool": "run_sql"}, {"tool": "run_sql"}]} for i in range(2)]
    s = summarize(rs)
    assert s["explorer"]["tools"] == {"describe_table": 1.0, "run_sql": 2.0}
    assert "delta" not in s["explorer"] and s["n"] == 2
    assert "explorer" in svg(s)


def test_critic_effect_is_summarised_on_the_same_answers():
    base = {"condition": "explorer_critic", "difficulty": "simple", "status": "ok", "cost": 0.001,
            "exhausted": False, "steps": [], "reverted": False}
    rs = [base | {"question_id": 1, "correct": True, "correct_before_critic": False, "critic": [{"verdict": "REVISE"}, {"verdict": "OK"}]},
          base | {"question_id": 2, "correct": False, "correct_before_critic": True, "critic": [{"verdict": "ERROR"}], "reverted": True},
          base | {"question_id": 3, "correct": True, "correct_before_critic": True, "critic": [{"verdict": "OK"}]},
          base | {"question_id": 4, "correct": False, "correct_before_critic": False, "critic": [{"verdict": "UNSURE"}]}]
    c = summarize(rs)["explorer_critic"]["critic"]
    assert c == {"verdicts": {"REVISE": 1, "ERROR": 1, "OK": 1, "UNSURE": 1}, "acted": 2, "fixed": 1,
                 "broke": 1, "missed": 1, "reverted": 1, "acc_before": 2 / 4}


def test_latency_median_and_p90_are_reported():
    rs = [{"question_id": i, "condition": "explorer", "difficulty": "simple", "status": "ok", "correct": True,
           "cost": 0.001, "exhausted": False, "steps": [], "seconds": float(i)} for i in range(1, 11)]
    assert summarize(rs)["explorer"]["seconds"] == {"median": 5.5, "p90": 9.0}
