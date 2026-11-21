"""Run the commit agent over the LangSmith dataset and score it.

Usage (venv active; GOOGLE_API_KEY, COMMIT_MODEL, LANGSMITH_API_KEY set):
  py run_eval.py --limit 3 --prefix smoke-test        # quick check on 3 examples
  py run_eval.py --prefix baseline-v1                 # full run on the dataset
"""
import argparse
import re
import uuid

from langsmith import Client, evaluate

from commit_agent import graph, llm, text_of, validate

TYPE_RE = re.compile(r"^(\w+)(\([^)]*\))?!?:")


def commit_type(subject: str) -> str:
    m = TYPE_RE.match(subject.strip())
    return m.group(1).lower() if m else ""


# ---------- target: runs the graph in auto mode (no interrupt, no real git commit) ----------
def target(inputs: dict) -> dict:
    result = graph.invoke(
        {"diff": inputs["diff"], "auto": True},
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )
    return {
        "message": result["final"],
        "attempts": result["attempts"],
        "remaining_issues": result.get("issues", []),
    }


# ---------- evaluators ----------
def format_valid(outputs: dict) -> dict:
    """Code evaluator: does the final message pass the deterministic format checks?"""
    issues = validate({"draft": outputs["message"]})["issues"]
    return {"key": "format_valid", "score": int(not issues), "comment": "; ".join(issues)}


def type_match(outputs: dict, reference_outputs: dict) -> dict:
    """Code evaluator: same Conventional Commits type (feat/fix/...) as the original?"""
    got = commit_type(outputs["message"].splitlines()[0])
    want = commit_type(reference_outputs["subject"])
    return {"key": "type_match", "score": int(got == want and bool(got)),
            "comment": f"agent={got or '-'} original={want or '-'}"}


def no_retry_needed(outputs: dict) -> dict:
    """Did the first draft pass validation (no trip through the loop)?"""
    return {"key": "no_retry_needed", "score": int(outputs["attempts"] == 1),
            "comment": f"attempts={outputs['attempts']}"}


def describes_change(inputs: dict, outputs: dict) -> dict:
    """LLM judge: does the message accurately describe what the diff does?"""
    prompt = (
        "You are reviewing a git commit message.\n\n"
        f"DIFF:\n{inputs['diff'][:6000]}\n\n"
        f"COMMIT MESSAGE:\n{outputs['message']}\n\n"
        "Does the message accurately and specifically describe the change in the diff "
        "(not vague, not claiming things the diff does not do)? "
        "Answer with one word, YES or NO, then a short reason."
    )
    answer = text_of(llm.invoke(prompt)).strip()
    return {"key": "describes_change", "score": int(answer.upper().startswith("YES")),
            "comment": answer[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="commit-agent-dataset")
    ap.add_argument("--prefix", required=True, help="experiment name prefix, e.g. baseline-v1")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N examples")
    args = ap.parse_args()

    data = args.dataset
    if args.limit:
        data = list(Client().list_examples(dataset_name=args.dataset, limit=args.limit))

    evaluate(
        target,
        data=data,
        evaluators=[format_valid, type_match, no_retry_needed, describes_change],
        experiment_prefix=args.prefix,
        max_concurrency=1,  # free Gemini tier: 15 requests/minute
    )


if __name__ == "__main__":
    main()
