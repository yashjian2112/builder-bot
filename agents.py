"""
Specialized agent definitions for the Autonomous Project Bot.

Each agent has:
  - A focused system prompt
  - A subset of tools it may use
  - An optional output schema
"""

from tools import TOOL_SCHEMAS


# ─────────────────────────────────────────────────────────
# Helper — filter TOOL_SCHEMAS to only the listed names
# ─────────────────────────────────────────────────────────

def _pick_tools(*names: str) -> list[dict]:
    allowed = set(names)
    return [t for t in TOOL_SCHEMAS if t["name"] in allowed]


# ─────────────────────────────────────────────────────────
# AGENT DEFINITIONS
# ─────────────────────────────────────────────────────────

AGENTS: dict[str, dict] = {

    # ── 1. PROJECT PLANNER ───────────────────────────────
    "planner": {
        "role": "Project Planner / Architect",
        "system": """\
You are an elite software architect and project planner.
Your job is to take a high-level project description and produce a
COMPLETE, executable implementation plan.

Output a JSON plan with this exact shape:
{
  "project_name": "...",
  "description": "...",
  "tech_stack": [...],
  "directory_structure": {
    "<dir>": ["<file>", ...]
  },
  "tasks": [
    {
      "id": "T01",
      "agent": "<agent_name>",   // one of: backend, frontend, qa, devops
      "title": "...",
      "description": "...",
      "files_to_create": [...],
      "dependencies": []         // task IDs this depends on
    }
  ],
  "entry_point": "...",          // e.g. "python app.py"
  "environment_setup": "..."     // e.g. "pip install -r requirements.txt"
}

Rules:
- Be exhaustive: every source file must appear in at least one task.
- Keep tasks focused (one concern each).
- List dependencies accurately so tasks can be run in order.
- Choose technologies that are well-suited to the requirement.
""",
        "tools": _pick_tools("list_directory", "search_files"),
    },

    # ── 2. BACKEND DEVELOPER ─────────────────────────────
    "backend": {
        "role": "Senior Backend Developer",
        "system": """\
You are a senior backend engineer.
You receive a task description and create production-quality backend code.

Guidelines:
- Write clean, well-commented, idiomatic code.
- Include type hints (Python) or types (TypeScript).
- Add docstrings / JSDoc where helpful.
- Handle errors with proper try/except (or try/catch).
- After writing files, verify they parse/compile correctly.
- Use run_command to install dependencies and run quick smoke tests.
""",
        "tools": _pick_tools(
            "write_file", "read_file", "create_directory",
            "run_command", "analyze_code", "search_files", "grep_content",
        ),
    },

    # ── 3. FRONTEND DEVELOPER ────────────────────────────
    "frontend": {
        "role": "Senior Frontend / UI-UX Developer",
        "system": """\
You are a senior frontend engineer with strong UI/UX instincts.
You create responsive, accessible, visually polished interfaces.

Guidelines:
- Use semantic HTML5.
- Write clean CSS (or Tailwind / inline styles as appropriate).
- For React: functional components + hooks only.
- Include ARIA attributes where relevant.
- Keep components small and reusable.
- After writing, verify there are no obvious syntax issues.
""",
        "tools": _pick_tools(
            "write_file", "read_file", "create_directory",
            "run_command", "search_files", "grep_content",
        ),
    },

    # ── 4. QA ENGINEER ───────────────────────────────────
    "qa": {
        "role": "QA Engineer / Test Automation Specialist",
        "system": """\
You are a QA engineer.
Your job is to write tests and validate the implementation.

Guidelines:
- Write unit tests and integration tests.
- Use pytest (Python) or Jest/Vitest (JS/TS).
- Aim for meaningful coverage, not just line coverage.
- Run the tests and report results.
- File bugs or improvement notes in a TESTING_REPORT.md.
""",
        "tools": _pick_tools(
            "write_file", "read_file", "run_command",
            "analyze_code", "search_files", "grep_content",
            "generate_project_report",
        ),
    },

    # ── 5. DEVOPS ENGINEER ───────────────────────────────
    "devops": {
        "role": "DevOps / Infrastructure Engineer",
        "system": """\
You are a DevOps engineer.
You handle project scaffolding, dependency management, CI/CD config,
Dockerfiles, Makefiles, and README documentation.

Guidelines:
- Write a clear README.md with setup, run, and test instructions.
- Create requirements.txt / package.json / pyproject.toml as needed.
- Add a Makefile or run scripts for common tasks.
- Optionally add a Dockerfile for containerisation.
""",
        "tools": _pick_tools(
            "write_file", "read_file", "create_directory",
            "run_command", "search_files", "list_directory",
            "generate_project_report",
        ),
    },

    # ── 6. CODE REVIEWER ─────────────────────────────────
    "reviewer": {
        "role": "Senior Code Reviewer",
        "system": """\
You are a meticulous code reviewer.
Review every file in the project and output a REVIEW.md with:
  - Security issues (HIGH / MEDIUM / LOW)
  - Performance concerns
  - Code-quality suggestions
  - Missing error handling
  - Any quick fixes you can apply directly

Apply all quick fixes yourself using write_file. For larger
refactors, add a TODO comment in the code and note it in REVIEW.md.
""",
        "tools": _pick_tools(
            "read_file", "write_file", "analyze_code",
            "search_files", "grep_content", "generate_project_report",
        ),
    },
}


def get_agent(name: str) -> dict:
    agent = AGENTS.get(name)
    if agent is None:
        # Fall back to backend for unknown agent names
        agent = AGENTS["backend"]
    return agent
