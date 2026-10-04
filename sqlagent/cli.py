import argparse
import json

from openai import OpenAI

from sqlagent.agent import agent
from sqlagent.db import db_file, overview, run


def main() -> None:
    p = argparse.ArgumentParser(description="Ask a BIRD mini-dev database a question.")
    p.add_argument("--db", required=True, help="db_id, e.g. california_schools")
    p.add_argument("--evidence", default="", help="optional hint, like BIRD's evidence field")
    p.add_argument("question")
    a = p.parse_args()

    path = db_file(a.db)
    r = agent(OpenAI(max_retries=5), a.question, a.evidence, overview(path), path, critic_rounds=2)
    for i, step in enumerate(r.steps, 1):
        print(f"--- step {i}: {step['tool']}({json.dumps(step['args'])})\n{step['output']}\n")
    for review in r.critic:
        print(f"--- critic: {review['verdict']} {review['feedback']}".rstrip() + "\n")
    if r.reverted:
        print("--- revision returned no rows; kept the previous answer\n")
    print(f"=== final{' (step cap hit)' if r.exhausted else ''}\n{r.sql}\n{run(path, r.sql)}\n")
    print(f"{r.input_tokens} in / {r.output_tokens} out tokens, ${r.cost:.5f}")


if __name__ == "__main__":
    main()
