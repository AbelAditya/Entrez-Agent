"""Trajectory eval runner for the Entrez agent.

Runs each eval query through the agent, then grades the tool-call trajectory it
produced against the reference trajectory in the eval file using an LLM judge.

Usage:
    python evals/evals.py --ids eval_02 eval_04  # a few evals, one run each
    python evals/evals.py --budget 12            # spend at most 12 model requests
    python evals/evals.py --score-only evals/runs.json  # grade saved runs, no agent calls

The free OpenRouter tier allows 20 requests/minute and 50 requests/day per key.
Requests are paced under the per-minute limit by ratelimits.py; the daily cap is
enforced here as a budget. One eval run costs 4-6 requests, so roughly 8-10 runs
fit in a day. Runs accumulate in runs.json; grade them once enough are collected.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

# Measure cold unless asked otherwise: a cached guardrail verdict would make a
# rerun cheaper than the real thing, and hide changes in guardrail behaviour.
# Set before importing agent, which builds its middleware (and their caches) at
# import time.
if "--cache" not in sys.argv:
    os.environ["ENTREZ_CACHE"] = "off"

from agentevals.trajectory.llm import (  # noqa: E402
    TRAJECTORY_ACCURACY_PROMPT_WITH_REFERENCE,
    create_async_trajectory_llm_as_judge,
)
from langchain.messages import HumanMessage  # noqa: E402
from langchain_core.messages.utils import convert_to_openai_messages  # noqa: E402
from langchain_openrouter import ChatOpenRouter  # noqa: E402

from agent import entrez_agent  # noqa: E402
from ratelimits import limiter  # noqa: E402

EVAL_FILE = Path(__file__).parent / "evals.json"
RUNS_FILE = Path(__file__).parent / "runs.json"
JUDGE_MODEL = "cohere/north-mini-code:free"
RECURSION_LIMIT = 24

# OpenRouter free tier allows 20 requests/minute and 50 requests/day per key.
# Pacing under 20/min is handled by the shared limiter in ratelimits.py; the
# daily cap is a budget, enforced here. One eval run spends one request per agent
# model round plus one per guardrail check, so a run typically costs 4-6, and the
# agent and judge share OPENROUTER_API_KEY against the same 50/day.
DAILY_REQUEST_BUDGET = 50
GUARDRAIL_REQUESTS_PER_RUN = 2  # ContentCheckMiddleware: input check + output check
ESTIMATED_REQUESTS_PER_RUN = 6  # worst case, used to decide whether to start a run


def requests_spent(run: dict) -> int:
    """Requests a completed run consumed: one per model round, plus guardrails."""
    if run.get("error"):
        return 1  # the run died on its first call (rate limit, crash)
    rounds = sum(1 for m in run.get("trajectory", []) if m.get("role") == "assistant")
    return rounds + GUARDRAIL_REQUESTS_PER_RUN


def build_reference(eval_case: dict) -> list[dict]:
    """Turn an eval's expected_trajectory into reference messages for the judge.

    Each expected step becomes an assistant message carrying one tool call. Steps
    without args are rendered with an empty argument set: the judge compares
    trajectories semantically, so a step's `purpose` matters more than its args.
    An empty expected_trajectory means the agent should answer without tools.
    """
    messages: list[dict] = [{"role": "user", "content": eval_case["query"]}]
    steps = eval_case.get("expected_trajectory") or []

    if not steps:
        messages.append(
            {
                "role": "assistant",
                "content": (
                    "Responds to the user without calling any tool. "
                    + eval_case.get("rationale", "")
                ),
            }
        )
        return messages

    for i, step in enumerate(steps):
        args = {k: v for k, v in step.get("args_must_include", {}).items()}
        messages.append(
            {
                "role": "assistant",
                "content": step.get("purpose", ""),
                "tool_calls": [
                    {
                        "id": f"ref_{i}",
                        "type": "function",
                        "function": {
                            "name": step["tool"],
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"ref_{i}", "content": "<result>"})

    messages.append({"role": "assistant", "content": "Answers the user from the results above."})
    return messages


def tool_call_summary(messages) -> list[dict]:
    """Compact record of what the agent actually called, for reading runs.json."""
    return [
        {"tool": tc["name"], "args": tc["args"]}
        for m in messages
        for tc in getattr(m, "tool_calls", []) or []
    ]


async def run_once(eval_case: dict) -> dict:
    """Invoke the agent on one query. Returns the trajectory, or an error."""
    started = time.time()
    try:
        result = await entrez_agent.ainvoke(
            {"messages": [HumanMessage(content=eval_case["query"])]},
            {"recursion_limit": RECURSION_LIMIT},
        )
    except Exception as exc:  # a crash or recursion limit is a failed run, not a crashed suite
        return {"error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - started, 1)}

    messages = result["messages"]
    return {
        "trajectory": convert_to_openai_messages(messages),
        "calls": tool_call_summary(messages),
        "final": str(messages[-1].content),
        "seconds": round(time.time() - started, 1),
    }


async def collect_runs(cases: list[dict], repeats: int, pause: float, budget: int, out: Path) -> list[dict]:
    """Run evals sequentially until the suite is done or the request budget runs out.

    Runs are saved after each one, so a budget stop (or a crash) leaves a usable
    file that `--score-only` can grade later.
    """
    runs: list[dict] = []
    spent = 0

    for case in cases:
        for attempt in range(1, repeats + 1):
            if spent + ESTIMATED_REQUESTS_PER_RUN > budget:
                print(f"\nstopping: {spent}/{budget} requests spent, not enough left for another run.")
                print(f"remaining evals: {', '.join(c['id'] for c in cases if not any(r['id'] == c['id'] for r in runs))}")
                return runs

            print(f"  running {case['id']} ({attempt}/{repeats}) [{spent}/{budget} requests]...", flush=True)
            run = await run_once(case)
            run |= {"id": case["id"], "attempt": attempt, "query": case["query"]}
            spent += requests_spent(run)

            if run.get("error"):
                print(f"    error: {run['error'][:100]}")
            else:
                print(f"    {len(run['calls'])} tool calls in {run['seconds']}s, ~{requests_spent(run)} requests")
            runs.append(run)
            out.write_text(json.dumps(runs, indent=2, default=str))
            if pause:
                await asyncio.sleep(pause)

    print(f"\n~{spent}/{budget} requests spent.")
    return runs


async def grade_runs(runs: list[dict], cases_by_id: dict, pause: float) -> list[dict]:
    """Grade each recorded trajectory against its reference with the LLM judge."""
    judge = create_async_trajectory_llm_as_judge(
        prompt=TRAJECTORY_ACCURACY_PROMPT_WITH_REFERENCE,
        judge=ChatOpenRouter(
            model=JUDGE_MODEL,
            temperature=0,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            rate_limiter=limiter("OPENROUTER_API_KEY"),
            max_retries=0,  # a retried 429 spends the daily budget without ever succeeding
        ),
    )

    graded = []
    for run in runs:
        if run.get("error"):
            graded.append(run | {"score": False, "comment": run["error"]})
            continue
        reference = build_reference(cases_by_id[run["id"]])
        try:
            verdict = await judge(outputs=run["trajectory"], reference_outputs=reference)
            graded.append(run | {"score": bool(verdict["score"]), "comment": verdict.get("comment", "")})
        except Exception as exc:
            graded.append(run | {"score": None, "comment": f"judge failed: {type(exc).__name__}: {exc}"})
        if pause:
            await asyncio.sleep(pause)
    return graded


def report(graded: list[dict], cases_by_id: dict) -> None:
    """Per-eval pass rate plus an aggregate, with the judge's reasoning on failures."""
    print("\n" + "=" * 72)
    passed_total = scored_total = 0

    for eval_id, case in cases_by_id.items():
        rows = [g for g in graded if g["id"] == eval_id]
        if not rows:
            continue
        scored = [r for r in rows if r["score"] is not None]
        passed = [r for r in scored if r["score"]]
        passed_total += len(passed)
        scored_total += len(scored)

        rate = f"{len(passed)}/{len(scored)}" if scored else "ungraded"
        print(f"{eval_id:36} {case['category']:22} {rate}")
        for row in rows:
            if row["score"] is not True and row.get("comment"):
                calls = " -> ".join(c["tool"] for c in row.get("calls", [])) or "no tool calls"
                print(f"    attempt {row['attempt']}: {calls}")
                print(f"      {row['comment'][:300]}")

    if scored_total:
        pct = 100 * passed_total / scored_total
        print("-" * 72)
        print(f"trajectory accuracy: {passed_total}/{scored_total} ({pct:.0f}%)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=EVAL_FILE, help="eval suite JSON")
    parser.add_argument("--repeats", type=int, default=1, help="runs per eval (raise only if the request budget allows)")
    parser.add_argument("--budget", type=int, default=DAILY_REQUEST_BUDGET, help="model requests this sweep may spend")
    parser.add_argument("--ids", nargs="*", help="only run these eval ids (prefix match)")
    parser.add_argument("--pause", type=float, default=0.0, help="extra seconds between runs (the shared limiter already paces requests)")
    parser.add_argument("--out", type=Path, default=RUNS_FILE, help="where to save raw runs")
    parser.add_argument("--score-only", type=Path, help="grade a saved runs file instead of calling the agent")
    parser.add_argument("--cache", action="store_true", help="allow guardrail verdict caching (off by default so runs are measured cold)")
    args = parser.parse_args()

    suite = json.loads(args.file.read_text())
    cases = suite["evals"]
    if args.ids:
        cases = [c for c in cases if any(c["id"].startswith(prefix) for prefix in args.ids)]
        if not cases:
            parser.error(f"no evals matched {args.ids}")
    cases_by_id = {c["id"]: c for c in cases}

    if args.score_only:
        runs = json.loads(args.score_only.read_text())
        runs = [r for r in runs if r["id"] in cases_by_id]
    else:
        wanted = len(cases) * args.repeats
        print(f"running {len(cases)} evals x {args.repeats} repeats against {suite['eval_suite']}")
        print(f"budget {args.budget} requests, ~{ESTIMATED_REQUESTS_PER_RUN}/run, so about "
              f"{args.budget // ESTIMATED_REQUESTS_PER_RUN} of {wanted} runs will fit today")
        runs = await collect_runs(cases, args.repeats, args.pause, args.budget, args.out)
        print(f"\nraw runs saved to {args.out} ({len(runs)}/{wanted} runs)")

    print(f"grading {len(runs)} runs with {JUDGE_MODEL}...")
    graded = await grade_runs(runs, cases_by_id, args.pause)
    report(graded, cases_by_id)


if __name__ == "__main__":
    asyncio.run(main())
