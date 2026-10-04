import argparse
import json

from openai import OpenAI

from sqlagent import catalog
from sqlagent.agent import agent
from sqlagent.db import Database, bird_sqlite


def main() -> None:
    p = argparse.ArgumentParser(description="Ask a SQL database a question in plain language.")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--db", help="BIRD db_id, e.g. california_schools (local SQLite copy)")
    where.add_argument("--url", help="SQLAlchemy URL, e.g. postgresql+psycopg://user:pw@host/db")
    p.add_argument("--catalog", help="JSON file describing tables and columns (see sqlagent/catalog.py)")
    p.add_argument("--tables", help="comma-separated tables the agent may see (default: all)")
    p.add_argument("--evidence", default="", help="optional hint, like BIRD's evidence field")
    p.add_argument("--critic", action="store_true", help="let a second model review the answer (off: it hurt on held-out)")
    p.add_argument("question")
    a = p.parse_args()

    tables = a.tables.split(",") if a.tables else None
    if a.db:
        db = bird_sqlite(a.db, tables=tables)
    else:
        db = Database(a.url, catalog=catalog.load(a.catalog) if a.catalog else None, tables=tables)
    r = agent(OpenAI(max_retries=5), a.question, a.evidence, db, critic_rounds=2 if a.critic else 0)
    for i, step in enumerate(r.steps, 1):
        print(f"--- step {i}: {step['tool']}({json.dumps(step['args'])})\n{step['output']}\n")
    for review in r.critic:
        print(f"--- critic: {review['verdict']} {review['feedback']}".rstrip() + "\n")
    if r.reverted:
        print("--- revision returned no rows; kept the previous answer\n")
    print(f"=== final{' (step cap hit)' if r.exhausted else ''}\n{r.sql}\n{db.run(r.sql)}\n")
    print(f"{r.input_tokens} in / {r.output_tokens} out tokens, ${r.cost:.5f}")


if __name__ == "__main__":
    main()
