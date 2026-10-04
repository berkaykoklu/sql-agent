import argparse
import json
import random
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from openai import OpenAI

from sqlagent.agent import agent, one_shot
from sqlagent.db import QUESTIONS, QueryError, db_file, execute, same_result, schema

CONDITIONS = ("one_shot", "one_shot_repeat", "agent", "agent_verify")
PILOT = {"simple": 6, "moderate": 10, "challenging": 4}


@lru_cache(maxsize=None)
def cached_schema(db_path: Path) -> str:
    return schema(db_path)


def pilot_sample(questions: list[dict]) -> list[dict]:
    by_difficulty = defaultdict(list)
    for q in questions:
        by_difficulty[q["difficulty"]].append(q)
    rng = random.Random(0)
    return [q for d, n in PILOT.items() for q in rng.sample(by_difficulty[d], n)]


def load_done(path: Path) -> set[tuple[int, str]]:
    done: set[tuple[int, str]] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            key = (row["question_id"], row["condition"])
            if row["status"] == "error":
                done.discard(key)
            else:
                done.add(key)
    return done


def evaluate(client, q: dict, condition: str, db_path: Path) -> dict:
    row = {k: q[k] for k in ("question_id", "db_id", "difficulty")} | {"condition": condition}
    try:
        gold = execute(db_path, q["SQL"])
    except QueryError as e:
        return row | {"status": "gold_failed", "error": str(e)}
    try:
        s = cached_schema(db_path)
        if condition.startswith("one_shot"):
            r = one_shot(client, q["question"], q["evidence"], s)
        else:
            r = agent(client, q["question"], q["evidence"], s, db_path, verify=condition == "agent_verify")
    except Exception as e:  # API failure after SDK retries; the next run retries this row
        return row | {"status": "error", "error": repr(e)}
    try:
        correct = bool(r.sql.strip()) and same_result(execute(db_path, r.sql), gold)
    except QueryError:
        correct = False
    return row | {
        "status": "ok", "correct": correct, "sql": r.sql, "steps": r.steps,
        "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
        "cost": r.cost, "exhausted": r.exhausted,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Run one_shot and agent on BIRD mini-dev.")
    p.add_argument("--pilot", action="store_true", help="20 questions stratified by difficulty")
    p.add_argument("--limit", type=int, help="first N questions only")
    p.add_argument("--out", default="results.jsonl")
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args()

    # mini-dev ships items 137 and 138 twice, byte-identical; 498 unique questions
    questions = list({q["question_id"]: q for q in json.loads(QUESTIONS.read_text())}.values())
    if a.pilot:
        questions = pilot_sample(questions)
    elif a.limit:
        questions = questions[: a.limit]

    out = Path(a.out)
    done = load_done(out)
    jobs = [(q, c) for q in questions for c in CONDITIONS if (q["question_id"], c) not in done]
    client = OpenAI(max_retries=5)
    lock = threading.Lock()

    def work(job: tuple[dict, str]) -> tuple[str, float]:
        q, condition = job
        row = evaluate(client, q, condition, db_file(q["db_id"]))
        with lock, out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row["status"], row.get("cost", 0.0)

    with ThreadPoolExecutor(a.workers) as pool:
        results = list(pool.map(work, jobs))
    print(dict(Counter(s for s, _ in results)), f"spend ${sum(c for _, c in results):.4f}", f"-> {out}")


if __name__ == "__main__":
    main()
