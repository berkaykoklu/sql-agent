import argparse
import json
import random
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from openai import OpenAI

from sqlagent.agent import agent
from sqlagent.db import QUESTIONS, Database, QueryError, bird, same_result

# critic off by default: on 300 held-out questions it fixed 2 answers and broke 5
CONDITIONS = ("explorer",)
CRITIC_ROUNDS = {"explorer": 0, "explorer_critic": 2}


@lru_cache(maxsize=None)
def database_for(db_id: str, url: str | None = None) -> Database:
    return bird(db_id, url)  # one per db_id, so each is reflected once


def sample_by_difficulty(questions: list[dict], n: int, seed: int = 0) -> list[dict]:
    by_difficulty = defaultdict(list)
    for q in questions:
        by_difficulty[q["difficulty"]].append(q)
    rng = random.Random(seed)
    return [q for group in by_difficulty.values()
            for q in rng.sample(group, round(n * len(group) / len(questions)))]


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


def evaluate(client, q: dict, condition: str, db: Database) -> dict:
    row = {k: q[k] for k in ("question_id", "db_id", "difficulty")} | {"condition": condition}
    try:
        gold = db.execute(q["SQL"])
    except QueryError as e:
        return row | {"status": "gold_failed", "error": str(e)}
    try:
        start = time.monotonic()
        r = agent(client, q["question"], q["evidence"], db, critic_rounds=CRITIC_ROUNDS[condition])
        seconds = time.monotonic() - start
    except Exception as e:  # API failure after SDK retries; the next run retries this row
        return row | {"status": "error", "error": repr(e)}
    row |= {
        "status": "ok", "correct": _matches(db, r.sql, gold), "sql": r.sql, "steps": r.steps,
        "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
        "cost": r.cost, "exhausted": r.exhausted, "seconds": round(seconds, 2),
    }
    if r.sql_before_critic is not None:
        row |= {"sql_before_critic": r.sql_before_critic, "critic": r.critic, "reverted": r.reverted,
                "correct_before_critic": _matches(db, r.sql_before_critic, gold)}
    return row


def _matches(db: Database, sql: str, gold: list[tuple]) -> bool:
    try:
        return bool(sql.strip()) and same_result(db.execute(sql), gold)
    except QueryError:
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="Run the explorer agent on BIRD mini-dev.")
    p.add_argument("--questions", default=str(QUESTIONS), help="question file in BIRD format")
    p.add_argument("--sample", type=int, help="N questions stratified by difficulty")
    p.add_argument("--limit", type=int, help="first N questions only")
    p.add_argument("--out", default="results.jsonl")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--critic", action="store_true", help="also run the critic (condition explorer_critic)")
    p.add_argument("--db-url", help="run on this database (e.g. PostgreSQL) instead of the BIRD SQLite files")
    p.add_argument("--label", help="condition name written to the rows (e.g. explorer_pg), so runs can be compared")
    a = p.parse_args()

    # mini-dev ships items 137 and 138 twice, byte-identical; 498 unique questions
    questions = list({q["question_id"]: q for q in json.loads(Path(a.questions).read_text())}.values())
    if a.sample:
        questions = sample_by_difficulty(questions, a.sample)
    elif a.limit:
        questions = questions[: a.limit]

    out = Path(a.out)
    done = load_done(out)
    conditions = ("explorer_critic",) if a.critic else CONDITIONS
    label = lambda c: a.label or c  # noqa: E731
    jobs = [(q, c) for q in questions for c in conditions if (q["question_id"], label(c)) not in done]
    client = OpenAI(max_retries=5)
    lock = threading.Lock()

    def work(job: tuple[dict, str]) -> tuple[str, float]:
        q, condition = job
        row = evaluate(client, q, condition, database_for(q["db_id"], a.db_url)) | {"condition": label(condition)}
        with lock, out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row["status"], row.get("cost", 0.0)

    with ThreadPoolExecutor(a.workers) as pool:
        results = list(pool.map(work, jobs))
    print(dict(Counter(s for s, _ in results)), f"spend ${sum(c for _, c in results):.4f}", f"-> {out}")


if __name__ == "__main__":
    main()
