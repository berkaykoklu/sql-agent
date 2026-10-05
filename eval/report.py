import argparse
import json
import random
import statistics
from collections import Counter
from pathlib import Path

BASELINE = "one_shot"
GROUPS = ("overall", "simple", "moderate", "challenging")


def summarize(rows: list[dict], n_boot: int = 10_000, seed: int = 0, baseline: str = BASELINE) -> dict:
    conditions = list(dict.fromkeys(r["condition"] for r in rows))
    last = {(r["question_id"], r["condition"]): r for r in rows}
    ok = {k: r for k, r in last.items() if r["status"] == "ok"}
    qids = sorted({q for q, _ in ok if all((q, c) in ok for c in conditions)})
    out: dict = {
        "conditions": conditions,
        "n": len(qids),
        "gold_failed": len({q for (q, _), r in last.items() if r["status"] == "gold_failed"}),
        "errors": sum(r["status"] == "error" for r in last.values()),
    }
    for c in conditions:
        picked = [ok[q, c] for q in qids]
        acc = {"overall": sum(r["correct"] for r in picked) / len(picked)}
        for d in GROUPS[1:]:
            group = [r for r in picked if r["difficulty"] == d]
            if group:
                acc[d] = sum(r["correct"] for r in group) / len(group)
        tools = Counter(s.get("tool", "run_sql") for r in picked for s in r["steps"])  # v1 steps had no "tool"
        out[c] = {
            "acc": acc,
            "cost": sum(r["cost"] for r in picked) / len(picked),
            "steps": sum(len(r["steps"]) for r in picked) / len(picked),
            "exhausted": sum(r["exhausted"] for r in picked),
            "tools": {k: v / len(picked) for k, v in tools.items()},
        }
        timed = sorted(r["seconds"] for r in picked if "seconds" in r)
        if timed:
            out[c]["seconds"] = {"median": statistics.median(timed), "p90": timed[int(0.9 * (len(timed) - 1))]}
        reviewed = [r for r in picked if "correct_before_critic" in r]
        if reviewed:
            acted = [any(v["verdict"] in ("REVISE", "ERROR") for v in r["critic"]) for r in reviewed]
            out[c]["critic"] = {
                "verdicts": dict(Counter(r["critic"][0]["verdict"] for r in reviewed)),
                "acted": sum(acted),
                "fixed": sum(r["correct"] and not r["correct_before_critic"] for r in reviewed),
                "broke": sum(r["correct_before_critic"] and not r["correct"] for r in reviewed),
                "missed": sum(not r["correct_before_critic"] and not a for r, a in zip(reviewed, acted)),
                "reverted": sum(r["reverted"] for r in reviewed),
                "acc_before": sum(r["correct_before_critic"] for r in reviewed) / len(reviewed),
            }
    n = len(qids)
    for c in conditions:
        if baseline not in conditions or c == baseline:
            continue
        diffs = [int(ok[q, c]["correct"]) - int(ok[q, baseline]["correct"]) for q in qids]
        rng = random.Random(seed)
        boots = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(n_boot))
        out[c]["delta"] = sum(diffs) / n
        out[c]["ci"] = (boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1])
    return out


def svg(summary: dict) -> str:
    conditions = summary["conditions"]
    height, pad, bar = 300, 40, 36
    width = 2 * pad + len(GROUPS) * (len(conditions) * bar + 30)
    palette = ["#9aa0a6", "#c8ccd0", "#1a73e8", "#0b8043", "#e37400"]
    colors = {c: palette[i % len(palette)] for i, c in enumerate(conditions)}
    parts = [f'<rect width="{width}" height="{height}" fill="#fff"/>']
    for i, g in enumerate(GROUPS):
        x0 = pad + i * (len(conditions) * bar + 30)
        for j, c in enumerate(conditions):
            acc = summary[c]["acc"].get(g, 0.0)
            h = acc * (height - 3 * pad)
            x, y = x0 + j * bar, height - pad - h
            parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bar - 6}" height="{h:.1f}" fill="{colors[c]}"/>')
            parts.append(f'<text x="{x + (bar - 6) / 2}" y="{y - 5:.1f}" text-anchor="middle">{acc:.0%}</text>')
        center = x0 + len(conditions) * bar / 2 - 3
        parts.append(f'<text x="{center}" y="{height - pad + 18}" text-anchor="middle">{g}</text>')
    for j, c in enumerate(conditions):
        parts.append(f'<rect x="{pad + j * 150}" y="12" width="12" height="12" fill="{colors[c]}"/>')
        parts.append(f'<text x="{pad + j * 150 + 18}" y="23">{c}</text>')
    body = "\n  ".join(parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'font-family="system-ui, sans-serif" font-size="13" fill="#202124">\n  {body}\n</svg>\n')


def main() -> None:
    p = argparse.ArgumentParser(description="Summarize one or more results files")
    p.add_argument("paths", nargs="*", default=["results.jsonl"])
    p.add_argument("--svg", default="docs/accuracy.svg")
    p.add_argument("--baseline", default=BASELINE, help="condition the others are compared with")
    a = p.parse_args()
    rows = [json.loads(line) for path in a.paths for line in Path(path).read_text().splitlines()]
    s = summarize(rows, baseline=a.baseline)
    print(f"paired questions: {s['n']}  gold_failed: {s['gold_failed']}  errors: {s['errors']}\n")
    print("| condition | " + " | ".join(GROUPS) + " | $/question | mean steps | step cap hit |")
    print("|---" * (len(GROUPS) + 4) + "|")
    for c in s["conditions"]:
        accs = " | ".join(f"{s[c]['acc'][g]:.1%}" if g in s[c]["acc"] else "–" for g in GROUPS)
        print(f"| {c} | {accs} | ${s[c]['cost']:.5f} | {s[c]['steps']:.2f} | {s[c]['exhausted']} |")
    print()
    for c in s["conditions"]:
        if s[c]["tools"]:
            print(f"{c} tools/question: " + " · ".join(f"{k} {v:.2f}" for k, v in sorted(s[c]["tools"].items())))
    for c in s["conditions"]:
        if "seconds" in s[c]:
            print(f"{c} seconds/question: median {s[c]['seconds']['median']:.1f}, p90 {s[c]['seconds']['p90']:.1f}")
    for c in s["conditions"]:
        if "critic" in s[c]:
            k = s[c]["critic"]
            print(f"{c} critic: accuracy before {k['acc_before']:.1%} → after {s[c]['acc']['overall']:.1%}; "
                  f"first verdicts {k['verdicts']}; acted {k['acted']}, fixed {k['fixed']}, broke {k['broke']}, "
                  f"missed {k['missed']}, reverted {k['reverted']}")
    for c in s["conditions"]:
        if "delta" in s[c]:
            low, high = s[c]["ci"]
            print(f"delta ({c} − {a.baseline}): {s[c]['delta']:+.1%}  95% CI [{low:+.1%}, {high:+.1%}]")
    Path(a.svg).write_text(svg(s))


if __name__ == "__main__":
    main()
