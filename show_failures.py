"""Summarize where an experiment failed.

Usage: py show_failures.py baseline-v1-36b876e2
"""
import re
import sys
from collections import Counter

from langsmith import Client

sys.stdout.reconfigure(encoding="utf-8")

name = sys.argv[1]
client = Client()
runs = list(client.list_runs(project_name=name, is_root=True))
print(f"{len(runs)} runs in experiment {name}\n")

feedback: dict = {}
for f in client.list_feedback(run_ids=[r.id for r in runs]):
    feedback.setdefault(f.run_id, {})[f.key] = f

pairs = Counter()
print("=== type_match failures (original  ->  agent) ===")
for r in runs:
    fb = feedback.get(r.id, {})
    tm = fb.get("type_match")
    if tm is not None and tm.score == 0:
        m = re.search(r"agent=(\S+) original=(\S+)", tm.comment or "")
        if m:
            pairs[(m.group(2), m.group(1))] += 1
        original = (r.reference_example_id and
                    client.read_example(r.reference_example_id).outputs.get("subject", ""))
        agent = (r.outputs or {}).get("message", "").splitlines()[0]
        print(f"  orig : {original}\n  agent: {agent}\n")

print("=== most common (original type -> agent type) ===")
for (orig, got), n in pairs.most_common():
    print(f"  {orig:10s} -> {got:10s} x{n}")

print("\n=== runs that needed the retry loop, or ended with format problems ===")
for r in runs:
    fb = feedback.get(r.id, {})
    nr, fv = fb.get("no_retry_needed"), fb.get("format_valid")
    if (nr is not None and nr.score == 0) or (fv is not None and fv.score == 0):
        out = r.outputs or {}
        print(f"  attempts={out.get('attempts')} remaining_issues={out.get('remaining_issues')}")
        print(f"  message: {out.get('message', '').splitlines()[0]}\n")
