from eval.make_heldout import heldout, unseen


def test_heldout_excludes_minidev_ids_and_repeated_question_texts():
    minidev = [{"question_id": 1, "question": "How many cards?", "difficulty": "simple"}]
    dev = ([{"question_id": 1, "question": "How many cards?", "difficulty": "simple"},
            {"question_id": 2, "question": "  how many CARDS? ", "difficulty": "simple"}]
           + [{"question_id": i, "question": f"q{i}", "difficulty": d}
              for i, d in zip(range(3, 103), ["simple"] * 50 + ["moderate"] * 30 + ["challenging"] * 20)])
    picked = heldout(dev, minidev, 10)
    assert {q["question_id"] for q in picked}.isdisjoint({1, 2})
    assert len(picked) == 10 and [q["difficulty"] for q in picked].count("simple") == 5
    assert picked == heldout(dev, minidev, 10)


def test_unseen_drops_minidev_ids_and_repeated_texts_deterministically():
    minidev = [{"question_id": 1, "question": "How many cards?"}]
    dev = [{"question_id": 1, "question": "other text"},
           {"question_id": 2, "question": "  how many CARDS? "},
           {"question_id": 3, "question": "How many sets?"}]
    assert [q["question_id"] for q in unseen(dev, minidev)] == [3]
