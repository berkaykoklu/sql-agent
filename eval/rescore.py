import argparse
import json
from pathlib import Path

from sqlagent.db import QueryError, db_file, execute, same_result

FIXES = Path(__file__).parent / "gold_fixes.json"


def rescore(rows: list[dict], fixes: dict[int, str], db_path_for) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        if r["status"] == "ok" and r["question_id"] in fixes:
            path = db_path_for(r["db_id"])
            try:
                gold = execute(path, fixes[r["question_id"]])
                r["correct"] = bool(r["sql"].strip()) and same_result(execute(path, r["sql"]), gold)
            except QueryError:
                r["correct"] = False
            r["gold_fixed"] = True
        out.append(r)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Re-score results against eval/gold_fixes.json; original files stay untouched")
    p.add_argument("paths", nargs="+")
    a = p.parse_args()
    fixes = {f["question_id"]: f["sql"] for f in json.loads(FIXES.read_text())}
    for path in map(Path, a.paths):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        out = path.with_suffix(".fixed.jsonl")
        out.write_text("".join(json.dumps(r) + "\n" for r in rescore(rows, fixes, db_file)))
        print(f"{path} -> {out}")


if __name__ == "__main__":
    main()
