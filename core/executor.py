"""
Task executor — runs tasks via Claude Code CLI (preferred) or Claude API (fallback).

Key design:
  • Builds a complete context document for every task
  • Uses --dangerously-skip-permissions so nothing blocks execution
  • Streams output back in real-time via an async generator
  • Falls back to Claude API with tool-use if CLI is not installed
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import AsyncGenerator

import anthropic

# Import the tools from the parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from tools import TOOL_SCHEMAS, execute_tool

HAS_CLAUDE_CLI = shutil.which("claude") is not None


async def stream_task(
    context_doc: str,
    project_dir: str,
    task: dict,
) -> AsyncGenerator[dict, None]:
    """
    Execute a single task and yield progress events:
      {"type": "log",      "message": "..."}
      {"type": "complete", "message": "..."}
      {"type": "error",    "message": "..."}
    """
    yield {"type": "log", "message": f"▶ Starting: {task['title']}"}

    if HAS_CLAUDE_CLI:
        async for event in _run_with_claude_cli(context_doc, project_dir):
            yield event
    else:
        async for event in _run_with_claude_api(context_doc, project_dir, task):
            yield event


# ─── Claude Code CLI path ────────────────────────────────

async def _run_with_claude_cli(
    context_doc: str,
    project_dir: str,
) -> AsyncGenerator[dict, None]:
    """Stream task execution via `claude --dangerously-skip-permissions`."""

    # Write context to a temp file (avoids shell escaping issues)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(context_doc)
        ctx_path = f.name

    prompt = (
        f"Read the context document at {ctx_path} carefully, "
        "then implement the CURRENT TASK completely. "
        "Work inside the PROJECT DIRECTORY specified in the document."
    )

    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "-p", prompt,
    ]

    yield {"type": "log", "message": "🔧 Claude Code CLI is running..."}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                yield {"type": "log", "message": text}

        await proc.wait()
        rc = proc.returncode

        if rc == 0:
            yield {"type": "complete", "message": "✅ Task completed successfully"}
        else:
            yield {"type": "error", "message": f"⚠ Claude Code exited with code {rc}"}

    except Exception as e:
        yield {"type": "error", "message": f"❌ CLI error: {e}"}
    finally:
        try:
            os.unlink(ctx_path)
        except Exception:
            pass


# ─── Claude API fallback path ────────────────────────────

async def _run_with_claude_api(
    context_doc: str,
    project_dir: str,
    task: dict,
) -> AsyncGenerator[dict, None]:
    """Execute task using Claude API + tool-use loop (no CLI required)."""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    yield {"type": "log", "message": "🤖 Running via Claude API (no CLI detected)..."}

    system = (
        "You are an expert software engineer implementing a specific task.\n"
        "You have full access to the file system via tools.\n"
        "Implement the CURRENT TASK completely. Work inside the project directory.\n"
        "After writing files, verify they are correct.\n"
        "You have all necessary permissions — proceed without asking."
    )

    prompt = context_doc

    # Only give tools relevant to the task type
    messages = [{"role": "user", "content": prompt}]

    for iteration in range(40):
        # Run in a thread pool to avoid blocking the event loop
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-opus-4-6",
                max_tokens=8192,
                thinking={"type": "adaptive"},
                system=system,
                tools=TOOL_SCHEMAS,
                messages=messages,
            ),
        )

        text_parts = []
        tool_uses = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if text_parts:
            preview = " ".join(text_parts)[:200].replace("\n", " ")
            yield {"type": "log", "message": f"💬 {preview}"}

        if response.stop_reason == "end_turn":
            yield {"type": "complete", "message": "✅ Task completed"}
            return

        if not tool_uses:
            yield {"type": "complete", "message": "✅ Task completed"}
            return

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tu in tool_uses:
            yield {"type": "log", "message": f"🔧 {tu.name}: {str(tu.input)[:80]}"}
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda tu=tu: execute_tool(tu.name, tu.input)
            )
            preview = str(result)[:120].replace("\n", " ")
            yield {"type": "log", "message": f"   ↳ {preview}"}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": str(result),
            })

        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error", "message": "⚠ Hit iteration limit"}
