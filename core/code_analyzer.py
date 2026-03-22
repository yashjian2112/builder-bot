"""
Existing codebase analyzer.
Reads a project folder, extracts structure and key info,
and produces a rich summary for the conversation bot.
"""

from __future__ import annotations

import os
import ast
import json
from pathlib import Path
from typing import Optional

# File types to read (skip binaries, lock files, etc.)
READABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".md", ".txt", ".env.example",
    ".sh", ".toml", ".cfg", ".ini", ".sql", ".graphql",
}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".next", "dist", "build", ".cache", "coverage", ".pytest_cache",
    "eggs", ".eggs", "*.egg-info",
}

MAX_FILE_SIZE = 50_000   # bytes — skip huge files
MAX_FILES = 200          # cap to avoid overwhelming context


def analyze_project(folder_path: str) -> dict:
    """
    Returns a dict with:
      - structure:   directory tree (string)
      - files:       list of {path, language, size, preview}
      - summary:     key stats
      - tech_stack:  detected technologies
    """
    root = Path(folder_path)
    if not root.exists():
        return {"error": f"Folder not found: {folder_path}"}

    files_data = []
    skipped = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # Skip unwanted dirs
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in READABLE_EXTENSIONS:
            continue
        if path.stat().st_size > MAX_FILE_SIZE:
            skipped.append(str(path.relative_to(root)))
            continue
        if len(files_data) >= MAX_FILES:
            break

        rel = str(path.relative_to(root))
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        files_data.append({
            "path": rel,
            "language": _detect_language(path),
            "size": len(content),
            "content": content,
        })

    tech_stack = _detect_tech_stack(files_data, root)
    structure  = _build_tree(root)
    summary    = _build_summary(files_data, root)

    # Build readable file map (truncated for large files)
    file_map = {}
    for f in files_data:
        preview = f["content"]
        if len(preview) > 3000:
            preview = preview[:3000] + f"\n\n... [{f['size']} total chars, truncated] ..."
        file_map[f["path"]] = preview

    return {
        "folder":     folder_path,
        "structure":  structure,
        "file_map":   file_map,
        "tech_stack": tech_stack,
        "summary":    summary,
        "skipped":    skipped,
    }


def _detect_language(path: Path) -> str:
    ext_map = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".jsx": "React JSX", ".tsx": "React TSX", ".html": "HTML",
        ".css": "CSS", ".scss": "SCSS", ".sql": "SQL",
        ".yaml": "YAML", ".yml": "YAML", ".json": "JSON",
        ".sh": "Shell", ".md": "Markdown",
    }
    return ext_map.get(path.suffix, path.suffix)


def _detect_tech_stack(files: list[dict], root: Path) -> list[str]:
    stack = set()
    all_content = " ".join(f["content"] for f in files)

    # Check for framework indicators
    indicators = {
        "FastAPI": ["from fastapi", "import fastapi", "FastAPI()"],
        "Flask": ["from flask", "import flask", "Flask(__name__)"],
        "Django": ["from django", "import django", "DJANGO_SETTINGS"],
        "React": ["import React", "from 'react'", "useState", "useEffect"],
        "Next.js": ["from 'next'", "getServerSideProps", "next/router"],
        "Vue.js": ["from 'vue'", "createApp", "Vue.component"],
        "Express": ["require('express')", "express()", "app.listen"],
        "SQLite": ["sqlite3", "SQLite", ".db"],
        "PostgreSQL": ["psycopg2", "postgresql", "postgres"],
        "MongoDB": ["pymongo", "mongoose", "mongodb"],
        "Redis": ["redis", "Redis"],
        "Docker": ["Dockerfile", "docker-compose"],
        "pytest": ["import pytest", "def test_"],
        "TypeScript": [".ts", ".tsx"],
        "Tailwind": ["tailwind", "tw-"],
        "Prisma": ["prisma", "@prisma"],
    }
    for tech, markers in indicators.items():
        if any(m in all_content for m in markers):
            stack.add(tech)

    # Check package files
    pkg = root / "package.json"
    if pkg.exists():
        stack.add("Node.js")
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for d in deps:
                if "react" in d: stack.add("React")
                if "next" in d: stack.add("Next.js")
                if "express" in d: stack.add("Express")
                if "typescript" in d: stack.add("TypeScript")
        except Exception:
            pass

    req = root / "requirements.txt"
    if req.exists():
        stack.add("Python")
        content = req.read_text()
        if "fastapi" in content.lower(): stack.add("FastAPI")
        if "flask" in content.lower(): stack.add("Flask")
        if "django" in content.lower(): stack.add("Django")

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        stack.add("Python")

    return sorted(stack)


def _build_tree(root: Path, indent: int = 0, max_depth: int = 4) -> str:
    lines = [str(root.name) + "/"]
    try:
        items = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return str(root.name) + "/"

    for item in items:
        if item.name in SKIP_DIRS or item.name.startswith("."):
            continue
        prefix = "  " * (indent + 1)
        if item.is_dir():
            if indent < max_depth - 1:
                subtree = _build_tree(item, indent + 1, max_depth)
                for line in subtree.splitlines():
                    lines.append(prefix + line)
            else:
                lines.append(prefix + item.name + "/")
        else:
            lines.append(prefix + item.name)

    return "\n".join(lines)


def _build_summary(files: list[dict], root: Path) -> dict:
    lang_count: dict[str, int] = {}
    total_lines = 0

    for f in files:
        lang = f["language"]
        lang_count[lang] = lang_count.get(lang, 0) + 1
        total_lines += f["content"].count("\n")

    return {
        "total_files": len(files),
        "total_lines": total_lines,
        "languages":   lang_count,
        "root_name":   root.name,
    }


def format_for_context(analysis: dict, max_chars: int = 40000) -> str:
    """
    Format the analysis into a human-readable context string
    suitable for pasting into a Claude prompt.
    """
    parts = []
    parts.append(f"## Existing Project: {analysis.get('folder', '')}\n")

    summary = analysis.get("summary", {})
    parts.append(
        f"**Stats:** {summary.get('total_files')} files, "
        f"~{summary.get('total_lines')} lines\n"
        f"**Languages:** {json.dumps(summary.get('languages', {}))}\n"
        f"**Detected stack:** {', '.join(analysis.get('tech_stack', []))}\n"
    )

    parts.append("\n## Directory Structure\n```\n" + analysis.get("structure", "") + "\n```\n")

    parts.append("\n## File Contents\n")
    chars_used = sum(len(p) for p in parts)
    for path, content in analysis.get("file_map", {}).items():
        entry = f"\n### {path}\n```\n{content}\n```\n"
        if chars_used + len(entry) > max_chars:
            parts.append(f"\n[... {path} omitted to stay within context limit ...]\n")
            continue
        parts.append(entry)
        chars_used += len(entry)

    return "".join(parts)
