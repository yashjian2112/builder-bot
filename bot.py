"""
Autonomous Project Bot
======================
Drives a complete software project from a one-line description to
runnable, tested code — with zero human interaction.

Architecture
------------
Orchestrator (claude-opus-4-6 + adaptive thinking)
  └─► Planner agent   → produces JSON task plan
  └─► Task agents     → backend / frontend / qa / devops
  └─► Reviewer agent  → final code review

Usage
-----
  python bot.py "Build a REST API for a todo app with SQLite"

Or set PROJECT_PROMPT env var and run without arguments.
"""

from __future__ import annotations

import os
import sys
import json
import time
import textwrap
from pathlib import Path
from datetime import datetime
from typing import Optional

import anthropic

from tools import TOOL_SCHEMAS, execute_tool
from agents import get_agent, AGENTS

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────

MODEL = "claude-opus-4-6"
MAX_TOKENS = 8192          # per agent call
MAX_TOOL_ITERATIONS = 30   # safety cap on tool-use loop
PROJECTS_ROOT = Path("./projects")


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def banner(text: str, char: str = "═") -> None:
    width = 70
    print(f"\n{char * width}")
    for line in textwrap.wrap(text, width - 4):
        print(f"  {line}")
    print(f"{char * width}\n")


def step(label: str, detail: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    msg = f"[{ts}] {label}"
    if detail:
        msg += f"  →  {detail}"
    print(msg)


def extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object from a text block."""
    # Try raw parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try finding ```json ... ```
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Last resort: find the first { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ─────────────────────────────────────────────────────────
# CORE: run one agent in an agentic tool-use loop
# ─────────────────────────────────────────────────────────

def run_agent(
    client: anthropic.Anthropic,
    agent_name: str,
    task_prompt: str,
    project_dir: Path,
    extra_context: str = "",
) -> str:
    """
    Run a specialised agent on a task.
    Returns the final text response from the agent.
    """
    agent = get_agent(agent_name)
    role  = agent["role"]
    tools = agent["tools"]

    system = (
        agent["system"]
        + f"\n\n## Project Directory\nAll files should be created inside: {project_dir}\n"
    )
    if extra_context:
        system += f"\n## Additional Context\n{extra_context}\n"

    messages = [{"role": "user", "content": task_prompt}]

    step(f"▶ {role}", task_prompt[:80] + ("…" if len(task_prompt) > 80 else ""))

    iteration = 0
    while iteration < MAX_TOOL_ITERATIONS:
        iteration += 1

        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=system,
            tools=tools,
            messages=messages,
        )

        # Collect text from the response
        response_text = ""
        tool_uses = []

        for block in response.content:
            if block.type == "text":
                response_text += block.text
            elif block.type == "tool_use":
                tool_uses.append(block)

        if response_text:
            # Print a truncated preview
            preview = response_text[:200].replace("\n", " ")
            step(f"  💬 {role}", preview + ("…" if len(response_text) > 200 else ""))

        # Done?
        if response.stop_reason == "end_turn":
            return response_text

        if not tool_uses:
            return response_text

        # Execute tools
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tu in tool_uses:
            step(f"  🔧 Tool: {tu.name}", str(tu.input)[:100])
            result = execute_tool(tu.name, tu.input)
            preview = str(result)[:150].replace("\n", " ")
            step(f"  ✅ Result", preview + ("…" if len(str(result)) > 150 else ""))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": str(result),
            })

        messages.append({"role": "user", "content": tool_results})

    return response_text  # hit iteration cap


# ─────────────────────────────────────────────────────────
# PLANNING PHASE
# ─────────────────────────────────────────────────────────

def plan_project(client: anthropic.Anthropic, requirement: str) -> dict:
    """Ask the planner agent to produce a JSON task plan."""
    banner("PHASE 1 — PROJECT PLANNING")

    prompt = (
        f"Project requirement:\n{requirement}\n\n"
        "Produce the full implementation plan as described in your instructions. "
        "Return ONLY the JSON object — no markdown fences, no prose before or after."
    )

    agent = get_agent("planner")
    messages = [{"role": "user", "content": prompt}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=agent["system"],
        tools=agent["tools"],
        messages=messages,
    )

    raw = next((b.text for b in response.content if b.type == "text"), "")
    plan = extract_json(raw)

    if not plan:
        # Try a second pass asking for pure JSON
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": "Please output the plan again as a single raw JSON object.",
        })
        response2 = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=agent["system"],
            messages=messages,
        )
        raw2 = next((b.text for b in response2.content if b.type == "text"), "")
        plan = extract_json(raw2)

    if not plan:
        raise ValueError(f"Planner did not return valid JSON.\nRaw output:\n{raw}")

    step("📋 Plan received",
         f"{len(plan.get('tasks', []))} tasks, "
         f"stack: {', '.join(plan.get('tech_stack', []))}")
    return plan


# ─────────────────────────────────────────────────────────
# EXECUTION PHASE
# ─────────────────────────────────────────────────────────

def execute_plan(
    client: anthropic.Anthropic,
    plan: dict,
    project_dir: Path,
) -> None:
    """Run each task in dependency order."""
    banner("PHASE 2 — TASK EXECUTION")

    tasks = plan.get("tasks", [])
    completed: set[str] = set()
    results: dict[str, str] = {}

    # Simple topological pass: retry up to len(tasks) times
    pending = list(tasks)
    for _ in range(len(tasks) + 1):
        if not pending:
            break
        next_pending = []
        for task in pending:
            deps = set(task.get("dependencies", []))
            if not deps.issubset(completed):
                next_pending.append(task)
                continue

            t_id    = task["id"]
            agent   = task.get("agent", "backend")
            title   = task["title"]
            desc    = task["description"]
            files   = task.get("files_to_create", [])

            banner(f"Task {t_id}: {title}", char="─")

            # Enrich task prompt with plan context
            context = (
                f"Plan summary:\n{plan.get('description', '')}\n\n"
                f"Tech stack: {', '.join(plan.get('tech_stack', []))}\n\n"
                f"Files to create/modify: {', '.join(files)}\n\n"
                f"Prior task outputs available: {list(completed)}"
            )

            task_prompt = (
                f"## Task {t_id}: {title}\n\n"
                f"{desc}\n\n"
                f"Files to create/modify:\n" +
                "\n".join(f"  - {project_dir / f}" for f in files)
            )

            result = run_agent(
                client,
                agent,
                task_prompt,
                project_dir,
                extra_context=context,
            )

            results[t_id] = result
            completed.add(t_id)
            step(f"✔ Task {t_id} complete")

        pending = next_pending

    if pending:
        step("⚠ Some tasks could not be completed due to unresolved dependencies",
             str([t["id"] for t in pending]))


# ─────────────────────────────────────────────────────────
# REVIEW PHASE
# ─────────────────────────────────────────────────────────

def review_project(
    client: anthropic.Anthropic,
    project_dir: Path,
    plan: dict,
) -> None:
    """Run the reviewer agent over the finished project."""
    banner("PHASE 3 — CODE REVIEW")

    prompt = (
        f"Please review all code in {project_dir}.\n"
        f"Project: {plan.get('description', 'See directory')}\n"
        f"Tech stack: {', '.join(plan.get('tech_stack', []))}\n\n"
        "Apply quick fixes directly. Write your findings to REVIEW.md."
    )

    run_agent(client, "reviewer", prompt, project_dir)
    step("✔ Review complete — see REVIEW.md")


# ─────────────────────────────────────────────────────────
# SETUP PHASE (devops)
# ─────────────────────────────────────────────────────────

def setup_project(
    client: anthropic.Anthropic,
    project_dir: Path,
    plan: dict,
) -> None:
    """Create README, requirements, Makefile, etc."""
    banner("PHASE 4 — PROJECT SETUP & DOCS")

    prompt = (
        f"Finalize the project in {project_dir}.\n"
        f"Entry point: {plan.get('entry_point', 'unknown')}\n"
        f"Environment setup: {plan.get('environment_setup', '')}\n"
        f"Tech stack: {', '.join(plan.get('tech_stack', []))}\n\n"
        "Create README.md, requirements.txt (or package.json), "
        "and a Makefile. Then run the environment setup command to "
        "verify everything installs correctly."
    )

    run_agent(client, "devops", prompt, project_dir)
    step("✔ Project setup complete")


# ─────────────────────────────────────────────────────────
# QA PHASE
# ─────────────────────────────────────────────────────────

def qa_project(
    client: anthropic.Anthropic,
    project_dir: Path,
    plan: dict,
) -> None:
    """Write and run tests."""
    banner("PHASE 5 — TESTING & QA")

    prompt = (
        f"Write comprehensive tests for the project in {project_dir}.\n"
        f"Entry point: {plan.get('entry_point', 'unknown')}\n"
        f"Tech stack: {', '.join(plan.get('tech_stack', []))}\n\n"
        "Run the tests and write a TESTING_REPORT.md with results."
    )

    run_agent(client, "qa", prompt, project_dir)
    step("✔ QA complete — see TESTING_REPORT.md")


# ─────────────────────────────────────────────────────────
# FINAL SUMMARY
# ─────────────────────────────────────────────────────────

def print_summary(project_dir: Path, plan: dict, start_time: float) -> None:
    elapsed = time.time() - start_time
    banner("🎉  PROJECT COMPLETE", char="★")
    print(f"  Project:    {plan.get('project_name', 'unnamed')}")
    print(f"  Directory:  {project_dir.resolve()}")
    print(f"  Entry:      {plan.get('entry_point', 'see README')}")
    print(f"  Setup:      {plan.get('environment_setup', 'see README')}")
    print(f"  Elapsed:    {elapsed:.0f}s")
    print()
    # List top-level files
    files = sorted(project_dir.glob("*"))
    if files:
        print("  Files created:")
        for f in files:
            print(f"    {'📁' if f.is_dir() else '📄'} {f.name}")
    print()


# ─────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────

def main() -> None:
    # ── Get project requirement ──────────────────────────
    requirement = (
        " ".join(sys.argv[1:]).strip()
        or os.environ.get("PROJECT_PROMPT", "").strip()
    )
    if not requirement:
        print("Usage:")
        print('  python bot.py "Build a REST API for a todo app with SQLite"')
        print()
        print("Or set PROJECT_PROMPT env var.")
        sys.exit(1)

    # ── API key ──────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌  ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # ── Create project directory ─────────────────────────
    slug = (
        requirement[:40]
        .lower()
        .replace(" ", "_")
        .replace("/", "_")
        .strip("_")
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_dir = PROJECTS_ROOT / f"{slug}_{ts}"
    project_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    banner(f"AUTONOMOUS PROJECT BOT\n{requirement}")

    # ── Save requirement ─────────────────────────────────
    (project_dir / "REQUIREMENT.md").write_text(
        f"# Project Requirement\n\n{requirement}\n\nStarted: {datetime.now().isoformat()}\n"
    )

    # ── Run all phases ───────────────────────────────────
    plan = plan_project(client, requirement)

    # Persist plan
    (project_dir / "PLAN.json").write_text(json.dumps(plan, indent=2))

    execute_plan(client, plan, project_dir)
    setup_project(client, project_dir, plan)
    qa_project(client, project_dir, plan)
    review_project(client, project_dir, plan)

    print_summary(project_dir, plan, start)


if __name__ == "__main__":
    main()
