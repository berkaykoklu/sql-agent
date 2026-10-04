import argparse
import json
from pathlib import Path

from eval.run import sample_by_difficulty
from sqlagent.db import QUESTIONS


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def unseen(dev: list[dict], minidev: list[dict]) -> list[dict]:
    seen_ids = {q["question_id"] for q in minidev}
    seen_texts = {_norm(q["question"]) for q in minidev}
    return [q for q in dev if q["question_id"] not in seen_ids and _norm(q["question"]) not in seen_texts]


def heldout(dev: list[dict], minidev: list[dict], n: int) -> list[dict]:
    return sample_by_difficulty(unseen(dev, minidev), n)


def main() -> None:
    p = argparse.ArgumentParser(description="Sample BIRD dev questions that are not in mini-dev")
    p.add_argument("dev_json")
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--out", default="data/heldout_300.json")
    a = p.parse_args()
    picked = heldout(json.loads(Path(a.dev_json).read_text()), json.loads(QUESTIONS.read_text()), a.n)
    Path(a.out).write_text(json.dumps(picked, indent=1))
    print(f"{len(picked)} questions -> {a.out}")


if __name__ == "__main__":
    main()
