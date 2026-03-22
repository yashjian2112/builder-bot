# Autonomous Project Bot

A fully autonomous software project builder powered by **Claude claude-opus-4-6**.
Give it a one-sentence requirement and it produces a complete, tested, reviewed codebase — no human interaction needed.

---

## How It Works

```
You → requirement
        │
        ▼
  [Planner Agent]  ←── claude-opus-4-6 + adaptive thinking
        │  produces JSON task plan
        ▼
  [Task Agents]    ←── run in dependency order
  │  backend   – writes API / business logic
  │  frontend  – builds UI / HTML / CSS / React
  │  qa        – writes & runs tests
  └─ devops    – README, requirements, Makefile
        │
        ▼
  [Reviewer Agent] ←── applies quick fixes, writes REVIEW.md
        │
        ▼
  [QA Agent]       ←── runs tests, writes TESTING_REPORT.md
        │
        ▼
  Complete project in ./projects/<name>/
```

Each agent runs in an autonomous **agentic tool-use loop**:
it can read/write files, execute shell commands, analyze code,
and search the project — until the task is done.

---

## Quick Start

### 1. Install dependencies

```bash
pip install anthropic
```

### 2. Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 3. Run

```bash
cd project_bot
python bot.py "Build a REST API for a todo app with SQLite and FastAPI"
```

Or use the env var:

```bash
PROJECT_PROMPT="Build a CLI expense tracker in Python" python bot.py
```

---

## Example Prompts

```bash
# Web API
python bot.py "Build a FastAPI REST API for a bookstore with SQLite, including CRUD endpoints and Swagger docs"

# CLI tool
python bot.py "Build a Python CLI tool that converts CSV files to JSON with filtering support"

# Full-stack
python bot.py "Build a simple task manager web app with HTML/CSS/JS frontend and Flask backend"

# Data processing
python bot.py "Build a Python script that fetches weather data from Open-Meteo API and plots temperature charts"
```

---

## Project Output

Every run creates a new folder in `./projects/`:

```
projects/
└── build_a_fastapi_rest_api_20240101_120000/
    ├── REQUIREMENT.md      ← your original prompt
    ├── PLAN.json           ← planner's task graph
    ├── README.md           ← generated docs
    ├── REVIEW.md           ← code review findings
    ├── TESTING_REPORT.md   ← test results
    ├── requirements.txt
    ├── Makefile
    ├── app/
    │   ├── main.py
    │   ├── models.py
    │   └── routes/
    └── tests/
        └── test_api.py
```

---

## Architecture

| File | Purpose |
|------|---------|
| `bot.py` | Main orchestrator — runs all phases |
| `agents.py` | Agent definitions (system prompts + tool subsets) |
| `tools.py` | Tool implementations (file I/O, shell, code analysis) |

### Available Tools

| Tool | Description |
|------|-------------|
| `write_file` | Create / overwrite files |
| `read_file` | Read file contents |
| `list_directory` | Tree view of a directory |
| `create_directory` | Create directories |
| `run_command` | Execute shell commands |
| `analyze_code` | Parse Python AST: functions, classes, imports |
| `search_files` | Glob-based file search |
| `grep_content` | Regex search across files |
| `generate_project_report` | Project-wide summary stats |

### Agents

| Agent | Responsibility |
|-------|---------------|
| `planner` | Produces the JSON task plan |
| `backend` | API, database, business logic |
| `frontend` | HTML, CSS, JS, React |
| `qa` | Tests, test runner, TESTING_REPORT.md |
| `devops` | README, requirements, Makefile, Docker |
| `reviewer` | Code review, quick fixes, REVIEW.md |

---

## Configuration

Edit the top of `bot.py`:

```python
MODEL = "claude-opus-4-6"   # model to use
MAX_TOKENS = 8192            # per agent call
MAX_TOOL_ITERATIONS = 30     # safety cap on tool-use loop
PROJECTS_ROOT = Path("./projects")
```

---

## Limitations & Tips

- **Python projects** work best out of the box; JS/TS projects need Node.js installed.
- Keep requirements specific: *"FastAPI todo API with SQLite"* works better than *"make an app"*.
- For large projects, increase `MAX_TOKENS` to `16000` and `MAX_TOOL_ITERATIONS` to `50`.
- Set `ANTHROPIC_API_KEY` in a `.env` file or your shell profile.
