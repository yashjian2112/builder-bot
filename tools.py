"""
Tool definitions for the Autonomous Project Bot.
These tools give Claude the ability to read/write files,
execute code, analyze structure, and manage projects.
"""

import os
import json
import subprocess
import ast
import re
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────
#  TOOL SCHEMAS  (passed to the Claude API)
# ─────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "write_file",
        "description": (
            "Write content to a file. Creates the file (and any missing parent "
            "directories) if it does not exist, or overwrites it if it does."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read and return the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories recursively (up to 3 levels deep).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: current dir)"},
            },
            "required": [],
        },
    },
    {
        "name": "create_directory",
        "description": "Create a directory (and any missing parents).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to create"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Execute a shell command and return stdout + stderr. "
            "Use for installing packages, running tests, linting, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 60)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "analyze_code",
        "description": (
            "Parse a Python file and return its structure: functions, classes, "
            "imports, and any obvious issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a Python source file"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
                "root": {"type": "string", "description": "Root directory (default: current dir)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_content",
        "description": "Search file contents for a regex pattern, returns matching lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "File or directory to search"},
                "file_glob": {"type": "string", "description": "Filter files by pattern, e.g. '*.py'"},
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "generate_project_report",
        "description": (
            "Generate a summary report of the current project state: "
            "file counts, languages detected, functions/classes found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Root directory of the project"},
            },
            "required": ["project_dir"],
        },
    },
]


# ─────────────────────────────────────────────────────────
#  TOOL IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────

def write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"✅ Written {len(content)} chars to '{path}'"
    except Exception as e:
        return f"❌ write_file error: {e}"


def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"❌ File not found: {path}"
    except Exception as e:
        return f"❌ read_file error: {e}"


def list_directory(path: str = ".") -> str:
    try:
        root = Path(path)
        if not root.exists():
            return f"❌ Path does not exist: {path}"
        lines = []
        for item in sorted(root.rglob("*")):
            depth = len(item.relative_to(root).parts)
            if depth > 3:
                continue
            indent = "  " * (depth - 1)
            icon = "📁" if item.is_dir() else "📄"
            lines.append(f"{indent}{icon} {item.name}")
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"❌ list_directory error: {e}"


def create_directory(path: str) -> str:
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return f"✅ Directory created: '{path}'"
    except Exception as e:
        return f"❌ create_directory error: {e}"


def run_command(command: str, cwd: str = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        parts = []
        if out:
            parts.append(f"STDOUT:\n{out}")
        if err:
            parts.append(f"STDERR:\n{err}")
        parts.append(f"Exit code: {result.returncode}")
        return "\n".join(parts) if parts else "(no output)"
    except subprocess.TimeoutExpired:
        return f"❌ Command timed out after {timeout}s"
    except Exception as e:
        return f"❌ run_command error: {e}"


def analyze_code(path: str) -> str:
    try:
        source = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source)

        imports, functions, classes, issues = [], [], [], []

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    imports.extend(a.name for a in node.names)
                else:
                    mod = node.module or ""
                    imports.append(f"{mod}.{', '.join(a.name for a in node.names)}")
            elif isinstance(node, ast.FunctionDef):
                args = [a.arg for a in node.args.args]
                functions.append(f"{node.name}({', '.join(args)}) [line {node.lineno}]")
            elif isinstance(node, ast.ClassDef):
                bases = [getattr(b, "id", "?") for b in node.bases]
                classes.append(
                    f"{node.name}({', '.join(bases)}) [line {node.lineno}]"
                )

        # Simple issues check
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            if "TODO" in line or "FIXME" in line or "HACK" in line:
                issues.append(f"Line {i}: {line.strip()}")

        report = {
            "file": path,
            "lines": len(lines),
            "imports": imports[:20],
            "functions": functions[:30],
            "classes": classes[:20],
            "issues": issues[:10],
        }
        return json.dumps(report, indent=2)
    except SyntaxError as e:
        return f"❌ Syntax error: {e}"
    except FileNotFoundError:
        return f"❌ File not found: {path}"
    except Exception as e:
        return f"❌ analyze_code error: {e}"


def search_files(pattern: str, root: str = ".") -> str:
    try:
        matches = list(Path(root).glob(pattern))
        if not matches:
            return f"No files found matching '{pattern}' in '{root}'"
        return "\n".join(str(m) for m in sorted(matches)[:50])
    except Exception as e:
        return f"❌ search_files error: {e}"


def grep_content(pattern: str, path: str, file_glob: str = "*") -> str:
    try:
        results = []
        root = Path(path)
        files = list(root.rglob(file_glob)) if root.is_dir() else [root]
        for f in sorted(files)[:20]:
            if not f.is_file():
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if re.search(pattern, line):
                        results.append(f"{f}:{i}: {line.strip()}")
            except Exception:
                continue
        return "\n".join(results[:100]) if results else f"No matches for '{pattern}'"
    except Exception as e:
        return f"❌ grep_content error: {e}"


def generate_project_report(project_dir: str) -> str:
    try:
        root = Path(project_dir)
        ext_count: dict[str, int] = {}
        total_lines = 0
        py_functions, py_classes = [], []

        for f in root.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix or "(no ext)"
            ext_count[ext] = ext_count.get(ext, 0) + 1
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
                total_lines += len(lines)
                if f.suffix == ".py":
                    tree = ast.parse("\n".join(lines))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.FunctionDef):
                            py_functions.append(f"{f.name}::{node.name}")
                        elif isinstance(node, ast.ClassDef):
                            py_classes.append(f"{f.name}::{node.name}")
            except Exception:
                pass

        report = {
            "project_dir": project_dir,
            "total_files": sum(ext_count.values()),
            "total_lines": total_lines,
            "file_types": dict(sorted(ext_count.items(), key=lambda x: -x[1])),
            "python_functions": py_functions[:30],
            "python_classes": py_classes[:20],
        }
        return json.dumps(report, indent=2)
    except Exception as e:
        return f"❌ generate_project_report error: {e}"


# ─────────────────────────────────────────────────────────
#  DISPATCHER
# ─────────────────────────────────────────────────────────

def execute_tool(name: str, inputs: dict[str, Any]) -> str:
    dispatch = {
        "write_file": lambda i: write_file(i["path"], i["content"]),
        "read_file": lambda i: read_file(i["path"]),
        "list_directory": lambda i: list_directory(i.get("path", ".")),
        "create_directory": lambda i: create_directory(i["path"]),
        "run_command": lambda i: run_command(
            i["command"], i.get("cwd"), i.get("timeout", 60)
        ),
        "analyze_code": lambda i: analyze_code(i["path"]),
        "search_files": lambda i: search_files(i["pattern"], i.get("root", ".")),
        "grep_content": lambda i: grep_content(
            i["pattern"], i["path"], i.get("file_glob", "*")
        ),
        "generate_project_report": lambda i: generate_project_report(i["project_dir"]),
    }
    fn = dispatch.get(name)
    if fn is None:
        return f"❌ Unknown tool: {name}"
    return fn(inputs)
