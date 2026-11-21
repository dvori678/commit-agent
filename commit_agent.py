"""Commit message agent - LangGraph.

Flow: get_diff -> (summarize if big) -> generate -> validate -> (loop) -> human_review -> finish

PROMPT_VERSION env var:
  v1 (default) = baseline prompt
  v2           = improved: type-selection rules + non-test files shown first + shorter subject target
"""
import os
import re
import subprocess
from typing import Literal, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.rate_limiters import InMemoryRateLimiter
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

MAX_ATTEMPTS = 3
MAX_DIFF_CHARS = 12_000
TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
PATTERN = re.compile(rf"^({TYPES})(\([\w\-./]+\))?!?: \S.*")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")

rate_limiter = InMemoryRateLimiter(requests_per_second=0.2, max_bucket_size=1)  # ~12 requests/min
llm = init_chat_model(
    os.getenv("COMMIT_MODEL", "openai:gpt-4o-mini"),
    temperature=0,
    rate_limiter=rate_limiter,
)


def text_of(msg) -> str:
    """Return plain text whether content is a str or a list of blocks (Gemini 3)."""
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(
        b if isinstance(b, str) else b.get("text", "")
        for b in content
        if isinstance(b, str) or b.get("type") == "text"
    )


class State(TypedDict, total=False):
    diff: str
    draft: str
    issues: list[str]
    attempts: int
    feedback: str
    final: str
    auto: bool  # True = evaluation mode: no interrupt, no real git commit


# ---------- v2 helpers ----------
TEST_PATH = re.compile(r"(^|/)(__tests__|tests?|spec)/|\.(test|spec)\.[jt]sx?$|_test\.py$", re.I)

TYPE_RULES = """Choose the commit TYPE by the purpose of the change to the product code, not by which files appear in the diff:
- fix: corrects a bug or wrong behavior (even if tests were added or changed alongside it)
- feat: adds a new user-visible capability
- refactor: restructures code with no behavior change
- perf: makes something faster or lighter
- docs: documentation only
- test: ONLY when test files are the only files changed
- build / ci / chore: tooling, dependencies, configuration
If both production code and tests changed, the tests only support the change: ignore them when choosing the type.
Keep the subject line at 60 characters or fewer."""


def _split_files(diff: str) -> list[str]:
    return [p for p in re.split(r"(?=^diff --git )", diff, flags=re.M) if p.strip()]


def _path(part: str) -> str:
    m = re.match(r"diff --git a/(\S+)", part)
    return m.group(1) if m else ""


def prepare_diff_v2(diff: str) -> str:
    """List the changed files by category and put production-code diffs before test diffs."""
    parts = _split_files(diff)
    if not parts:
        return diff
    src = [p for p in parts if not TEST_PATH.search(_path(p))]
    tests = [p for p in parts if TEST_PATH.search(_path(p))]
    header = (
        "FILES CHANGED\n"
        f"production/other files: {', '.join(_path(p) for p in src) or 'none'}\n"
        f"test files: {', '.join(_path(p) for p in tests) or 'none'}\n\n"
    )
    return header + "".join(src + tests)


# ---------- nodes ----------
def get_diff(state: State) -> dict:
    if state.get("diff"):  # in evaluation the diff is supplied by the dataset
        return {"attempts": 0}
    out = subprocess.run(
        ["git", "diff", "--staged"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return {"diff": out.stdout, "attempts": 0}


def route_after_diff(state: State) -> Literal["summarize", "generate"]:
    return "summarize" if len(state["diff"]) > MAX_DIFF_CHARS else "generate"


def summarize(state: State) -> dict:
    """Big diff: summarize per file first, then generate from the summaries."""
    parts = re.split(r"(?=^diff --git )", state["diff"], flags=re.M)
    summaries = []
    for part in filter(None, parts):
        msg = llm.invoke(
            "Summarize this single-file git diff in 2 short sentences:\n\n"
            + part[:MAX_DIFF_CHARS]
        )
        summaries.append(text_of(msg))
    return {"diff": "Per-file summary of a large diff:\n- " + "\n- ".join(summaries)}


def generate(state: State) -> dict:
    diff = prepare_diff_v2(state["diff"]) if PROMPT_VERSION == "v2" else state["diff"]
    prompt = (
        "Write a git commit message in Conventional Commits format "
        "(type(scope): subject). Subject max 72 chars, imperative mood, "
        "no trailing period. Optional short body after a blank line. "
        "Output only the message.\n\n"
    )
    if PROMPT_VERSION == "v2":
        prompt += TYPE_RULES + "\n\n"
    prompt += f"DIFF:\n{diff}\n"
    if state.get("issues"):
        prompt += f"\nYour previous draft was:\n{state['draft']}\nFix these problems: {state['issues']}\n"
    if state.get("feedback"):
        prompt += f"\nUser feedback to apply: {state['feedback']}\n"
    msg = llm.invoke(prompt)
    return {"draft": text_of(msg).strip(), "attempts": state.get("attempts", 0) + 1}


def validate(state: State) -> dict:
    """Deterministic check - no LLM."""
    issues = []
    subject = state["draft"].splitlines()[0] if state["draft"] else ""
    if not PATTERN.match(subject):
        issues.append("subject must match 'type(scope): description' with a valid type")
    if len(subject) > 72:
        issues.append(f"subject is {len(subject)} chars, max is 72")
    if subject.endswith("."):
        issues.append("subject must not end with a period")
    return {"issues": issues}


def route_after_validate(state: State) -> Literal["generate", "human_review"]:
    if state["issues"] and state["attempts"] < MAX_ATTEMPTS:
        return "generate"
    return "human_review"


def human_review(state: State) -> Command[Literal["finish", "generate"]]:
    if state.get("auto"):
        return Command(update={"final": state["draft"]}, goto="finish")

    decision = interrupt({"draft": state["draft"], "issues": state.get("issues", [])})
    action = decision["action"]
    if action == "approve":
        return Command(update={"final": state["draft"]}, goto="finish")
    if action == "edit":
        return Command(update={"final": decision["text"]}, goto="finish")
    # reject: go back to generate with the user's note, reset the attempts counter
    return Command(
        update={"feedback": decision.get("note", ""), "issues": [], "attempts": 0},
        goto="generate",
    )


def finish(state: State) -> dict:
    if not state.get("auto"):
        subprocess.run(["git", "commit", "-m", state["final"]], check=True)
    return {}


# ---------- graph ----------
def build_graph():
    g = StateGraph(State)
    g.add_node("get_diff", get_diff)
    g.add_node("summarize", summarize)
    g.add_node("generate", generate)
    g.add_node("validate", validate)
    g.add_node("human_review", human_review)
    g.add_node("finish", finish)

    g.add_edge(START, "get_diff")
    g.add_conditional_edges("get_diff", route_after_diff)
    g.add_edge("summarize", "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges("validate", route_after_validate)
    g.add_edge("finish", END)
    return g.compile(checkpointer=MemorySaver())  # interrupt requires a checkpointer


graph = build_graph()


# ---------- CLI ----------
def main():
    config = {"configurable": {"thread_id": "commit-1"}}
    result = graph.invoke({}, config)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print("\nProposed message:\n" + payload["draft"])
        if payload["issues"]:
            print("Remaining issues:", payload["issues"])
        choice = input("\n[a]pprove / [e]dit / [r]eject: ").strip().lower()
        if choice == "a":
            resume = {"action": "approve"}
        elif choice == "e":
            resume = {"action": "edit", "text": input("New message: ")}
        else:
            resume = {"action": "reject", "note": input("What to change? ")}
        result = graph.invoke(Command(resume=resume), config)
    print("Done.")


if __name__ == "__main__":
    main()
