import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from sqlagent.db import QUESTIONS


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def unseen(dev: list[dict], minidev: list[dict]) -> list[dict]:
    seen_ids = {q["question_id"] for q in minidev}
    seen_texts = {_norm(q["question"]) for q in minidev}
    return [q for q in dev if q["question_id"] not in seen_ids and _norm(q["question"]) not in seen_texts]


# dev is 60% simple; fixed quotas keep enough moderate and challenging questions to report on
QUOTAS = {"simple": 150, "moderate": 107, "challenging": 43}


def heldout(dev: list[dict], minidev: list[dict], quotas: dict[str, int], seed: int = 0) -> list[dict]:
    groups = defaultdict(list)
    for q in unseen(dev, minidev):
        groups[q["difficulty"]].append(q)
    rng = random.Random(seed)
    return [q for d, n in quotas.items() for q in rng.sample(groups[d], min(n, len(groups[d])))]


def main() -> None:
    p = argparse.ArgumentParser(description="Sample BIRD dev questions that are not in mini-dev")
    p.add_argument("dev_json")
    p.add_argument("--out", default="data/heldout_300.json")
    a = p.parse_args()
    picked = heldout(json.loads(Path(a.dev_json).read_text()), json.loads(QUESTIONS.read_text()), QUOTAS)
    Path(a.out).write_text(json.dumps(picked, indent=1))
    print(f"{len(picked)} questions -> {a.out}")


if __name__ == "__main__":
    main()
