"""Export (diff, original commit message) pairs from local git repos, optionally upload to LangSmith.

Usage (from the commit-agent folder, venv active):
  py export_dataset.py D:\\path\\repo1 D:\\path\\repo2 --max-per-repo 15
  py export_dataset.py D:\\path\\repo1 D:\\path\\repo2 --upload --name commit-agent-dataset

Step 1 (no --upload) only writes commits.jsonl so you can inspect it first.
"""
import argparse
import json
import re
import subprocess
import sys

CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([\w\-./]+\))?!?: \S"
)
EXCLUDES = [
    ":(exclude)package-lock.json",
    ":(exclude)yarn.lock",
    ":(exclude)pnpm-lock.yaml",
    ":(exclude)*.lock",
    ":(exclude)*.min.js",
    ":(exclude)*.min.css",
    ":(exclude)*.svg",
    ":(exclude)*.png",
    ":(exclude)*.jpg",
]
SKIP_SUBJECTS = re.compile(r"^(merge|initial commit|first commit|release|chore\(deps\)|chore: update)", re.I)

def git(repo: str, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return out.stdout


def export_repo(repo: str, limit: int, max_chars: int) -> list[dict]:
    hashes = git(repo, "rev-list", "--no-merges", "HEAD").split()
    rows = []
    for h in hashes:
        if len(rows) >= limit:
            break
        message = git(repo, "show", "-s", "--format=%B", h).strip()
        subject = message.splitlines()[0] if message else ""
        if not subject or SKIP_SUBJECTS.match(subject):
            continue
        diff = git(repo, "show", "--format=", "--patch", h, "--", ".", *EXCLUDES)
        if not diff.strip() or len(diff) > max_chars:
            continue
        rows.append({
            "repo": repo.rstrip("\\/").split("\\")[-1].split("/")[-1],
            "hash": h[:10],
            "diff": diff,
            "message": message,
            "subject": subject,
            "is_conventional": bool(CONVENTIONAL.match(subject)),
        })
    return rows


def upload(rows: list[dict], name: str) -> None:
    from langsmith import Client

    client = Client()
    if client.has_dataset(dataset_name=name):
        print(f"Dataset '{name}' already exists - pick another --name or delete it in LangSmith.")
        sys.exit(1)
    ds = client.create_dataset(name, description="Real git diffs with their original commit messages")
    client.create_examples(
        dataset_id=ds.id,
        inputs=[{"diff": r["diff"]} for r in rows],
        outputs=[{"message": r["message"], "subject": r["subject"],
                  "is_conventional": r["is_conventional"]} for r in rows],
        metadata=[{"repo": r["repo"], "hash": r["hash"]} for r in rows],
    )
    print(f"Uploaded {len(rows)} examples to dataset '{name}'.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--max-per-repo", type=int, default=15)
    ap.add_argument("--max-diff-chars", type=int, default=8000)
    ap.add_argument("--out", default="commits.jsonl")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--name", default="commit-agent-dataset")
    args = ap.parse_args()

    rows = []
    for repo in args.repos:
        got = export_repo(repo, args.max_per_repo, args.max_diff_chars)
        print(f"{repo}: {len(got)} commits exported")
        rows += got

    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    conv = sum(r["is_conventional"] for r in rows)
    print(f"\nTotal: {len(rows)} commits ({conv} in Conventional Commits format). Written to {args.out}")

    if args.upload:
        upload(rows, args.name)


if __name__ == "__main__":
    main()
