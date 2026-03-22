"""
FastAPI server — handles WebSocket chat, REST API, file serving.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.memory import (
    init_db, create_session, get_session, list_sessions,
    update_session, add_message, get_conversation_history,
)
from core.conversation import ConversationManager
from core.code_analyzer import analyze_project, format_for_context
from core.executor import stream_task, HAS_CLAUDE_CLI

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
MOCKUP_DIR = DATA_DIR / "mockups"
SESSIONS_DIR = DATA_DIR / "sessions"

MOCKUP_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

init_db()

app = FastAPI(title="Builder Bot")

# Serve static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ─── WebSocket connection pool ───────────────────────────

class WsPool:
    def __init__(self):
        self._pool: dict[str, WebSocket] = {}

    async def add(self, sid: str, ws: WebSocket):
        await ws.accept()
        self._pool[sid] = ws

    def remove(self, sid: str):
        self._pool.pop(sid, None)

    async def send(self, sid: str, data: dict):
        ws = self._pool.get(sid)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.remove(sid)

pool = WsPool()


# ─── REST endpoints ──────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/sessions")
async def api_list_sessions():
    return list_sessions()


@app.post("/api/sessions")
async def api_create_session():
    sid = create_session()
    return {"session_id": sid}


@app.get("/api/session/{sid}")
async def api_get_session(sid: str):
    s = get_session(sid)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    history = get_conversation_history(sid)
    return {**s, "history": history}


@app.get("/mockup/{sid}/{name}")
async def serve_mockup(sid: str, name: str):
    path = MOCKUP_DIR / sid / name
    if not path.exists():
        return HTMLResponse("<h1>Mockup not found</h1>", status_code=404)
    return FileResponse(str(path), media_type="text/html")


# ─── WebSocket ───────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    await pool.add(session_id, websocket)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        await pool.send(session_id, {
            "type": "error",
            "message": "ANTHROPIC_API_KEY is not set. Please set it and restart."
        })
        pool.remove(session_id)
        return

    conv = ConversationManager(api_key)

    # Send welcome if first time
    session = get_session(session_id)
    if not session:
        session_id = create_session()
        session = get_session(session_id)

    history = get_conversation_history(session_id)
    if not history:
        welcome = (
            "👋 Hey! I'm **Builder Bot** — your autonomous project engineer.\n\n"
            "I'll help you go from idea → UI design → working code, step by step.\n\n"
            "Tell me: are you starting a **new project**, or do you want to enhance an **existing app**?"
        )
        add_message(session_id, "assistant", welcome)
        await pool.send(session_id, {
            "type": "message",
            "role": "assistant",
            "content": welcome,
            "phase": "greeting",
        })
    else:
        # Reconnect — send session state
        await pool.send(session_id, {
            "type": "session_restored",
            "phase": session.get("phase", "greeting"),
        })

    # Pending approval state (in-memory during session)
    pending_approval: dict = {}

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")

            # ── User chat message ────────────────────────
            if msg_type == "message":
                user_text = data.get("content", "").strip()
                if not user_text:
                    continue

                add_message(session_id, "user", user_text)
                session = get_session(session_id)
                phase = session.get("phase", "greeting")
                history = get_conversation_history(session_id)

                # Show typing indicator
                await pool.send(session_id, {"type": "thinking"})

                # Build extra context if we have code analysis
                extra = ""
                code_analysis = session.get("tech_stack", "")  # reuse field for raw analysis

                # Get the response from Claude
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: conv.chat(
                        session_id,
                        history[:-1],  # exclude last user msg (already appended)
                        user_text,
                        extra_context=extra,
                        phase=phase,
                    ),
                )

                reply = result["reply"]
                signals = result["signals"]

                add_message(session_id, "assistant", reply)

                await pool.send(session_id, {
                    "type": "message",
                    "role": "assistant",
                    "content": reply,
                    "phase": phase,
                })

                # ── Handle signals ───────────────────────

                if signals.get("need_folder"):
                    update_session(session_id, phase="awaiting_folder")

                if signals.get("requirements"):
                    req = signals["requirements"]
                    update_session(
                        session_id,
                        requirements=json.dumps(req),
                        name=req.get("project_name", session["name"]),
                        phase="requirements_done",
                    )
                    await pool.send(session_id, {
                        "type": "phase_change",
                        "phase": "requirements_done",
                        "data": req,
                    })

                if signals.get("mockup_html"):
                    html = signals["mockup_html"]
                    mockup_path = MOCKUP_DIR / session_id
                    mockup_path.mkdir(parents=True, exist_ok=True)
                    (mockup_path / "mockup.html").write_text(html, encoding="utf-8")

                    update_session(session_id, phase="awaiting_ui_approval")
                    pending_approval["ui"] = {"html": html}

                    await pool.send(session_id, {
                        "type": "approval_request",
                        "approval_type": "ui",
                        "approval_id": "ui_mockup",
                        "title": "UI/UX Mockup Ready",
                        "description": "Your UI mockup is ready. Preview it and then approve or request changes.",
                        "preview_url": f"/mockup/{session_id}/mockup.html",
                    })

                if signals.get("task_plan"):
                    plan = signals["task_plan"]
                    update_session(
                        session_id,
                        task_plan=json.dumps(plan),
                        phase="awaiting_task_approval",
                    )
                    pending_approval["tasks"] = plan

                    await pool.send(session_id, {
                        "type": "approval_request",
                        "approval_type": "tasks",
                        "approval_id": "task_plan",
                        "title": f"Implementation Plan ({len(plan)} tasks)",
                        "description": "Here's the complete implementation plan. Approve to start building.",
                        "data": plan,
                    })

            # ── Folder path provided ─────────────────────
            elif msg_type == "set_folder":
                folder = data.get("path", "").strip()
                source = data.get("source", "local")
                await pool.send(session_id, {"type": "thinking"})

                # Handle GitHub URL
                if source == "github" or folder.startswith("https://github.com"):
                    await pool.send(session_id, {
                        "type": "message",
                        "role": "assistant",
                        "content": f"Fetching GitHub repository `{folder}`... this may take a moment.",
                        "phase": "code_analysis",
                    })
                    import tempfile, subprocess as sp
                    tmp_dir = tempfile.mkdtemp(prefix="builder_bot_")
                    clone_result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: sp.run(
                            ["git", "clone", "--depth=1", folder, tmp_dir],
                            capture_output=True, text=True
                        )
                    )
                    if clone_result.returncode != 0:
                        await pool.send(session_id, {
                            "type": "message",
                            "role": "assistant",
                            "content": f"Could not clone that repository.\n\nError: {clone_result.stderr[:300]}\n\nMake sure the repo is public and the URL is correct.",
                            "phase": "awaiting_folder",
                        })
                        continue
                    folder = tmp_dir
                else:
                    await pool.send(session_id, {
                        "type": "message",
                        "role": "assistant",
                        "content": f"Analyzing your codebase at `{folder}`... this may take a moment.",
                        "phase": "code_analysis",
                    })

                analysis = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: analyze_project(folder)
                )

                if "error" in analysis:
                    await pool.send(session_id, {
                        "type": "message",
                        "role": "assistant",
                        "content": f"Could not read that folder: {analysis['error']}\n\nPlease check the path and try again.",
                        "phase": "awaiting_folder",
                    })
                    continue

                formatted = format_for_context(analysis)
                update_session(
                    session_id,
                    project_folder=folder,
                    tech_stack=formatted,
                    phase="code_analysis_done",
                )

                # Tell the AI what we found
                summary = analysis["summary"]
                stack   = ", ".join(analysis.get("tech_stack", ["unknown"]))

                system_note = (
                    f"I've analyzed the project. Here's what I found:\n\n"
                    f"**{summary['total_files']} files**, ~{summary['total_lines']} lines\n"
                    f"**Stack detected:** {stack}\n"
                    f"**Languages:** {json.dumps(summary.get('languages', {}))}\n\n"
                    f"I have full access to all the file contents."
                )

                history = get_conversation_history(session_id)
                add_message(session_id, "user", system_note)

                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: conv.chat(
                        session_id,
                        history,
                        "Based on this analysis, what are your first questions for me?",
                        extra_context=formatted,
                        phase="code_analysis_done",
                    ),
                )

                reply = result["reply"]
                add_message(session_id, "assistant", reply)
                await pool.send(session_id, {
                    "type": "message",
                    "role": "assistant",
                    "content": reply,
                    "phase": "code_analysis_done",
                })

            # ── Approval / rejection ─────────────────────
            elif msg_type == "approval":
                approval_id = data.get("approval_id")
                decision    = data.get("decision")   # "approve" or "reject"
                feedback    = data.get("feedback", "")

                session = get_session(session_id)

                if decision == "reject":
                    # Feed feedback back into conversation
                    reject_msg = feedback or "Please revise this."
                    add_message(session_id, "user", reject_msg)
                    history = get_conversation_history(session_id)

                    await pool.send(session_id, {"type": "thinking"})

                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: conv.chat(
                            session_id,
                            history[:-1],
                            reject_msg,
                            phase=session.get("phase", ""),
                        ),
                    )
                    reply = result["reply"]
                    signals = result["signals"]
                    add_message(session_id, "assistant", reply)

                    await pool.send(session_id, {
                        "type": "message",
                        "role": "assistant",
                        "content": reply,
                        "phase": session.get("phase"),
                    })

                    # Re-check for new mockup/plan in revised response
                    if signals.get("mockup_html"):
                        html = signals["mockup_html"]
                        mockup_path = MOCKUP_DIR / session_id
                        mockup_path.mkdir(parents=True, exist_ok=True)
                        (mockup_path / "mockup.html").write_text(html, encoding="utf-8")
                        await pool.send(session_id, {
                            "type": "approval_request",
                            "approval_type": "ui",
                            "approval_id": "ui_mockup",
                            "title": "Updated UI Mockup",
                            "description": "Here's the revised mockup.",
                            "preview_url": f"/mockup/{session_id}/mockup.html",
                        })

                    if signals.get("task_plan"):
                        plan = signals["task_plan"]
                        update_session(session_id, task_plan=json.dumps(plan))
                        pending_approval["tasks"] = plan
                        await pool.send(session_id, {
                            "type": "approval_request",
                            "approval_type": "tasks",
                            "approval_id": "task_plan",
                            "title": "Revised Implementation Plan",
                            "description": "Here's the updated plan.",
                            "data": plan,
                        })

                elif decision == "approve":
                    if approval_id == "ui_mockup":
                        update_session(session_id, phase="ui_approved")
                        await pool.send(session_id, {
                            "type": "message",
                            "role": "assistant",
                            "content": "UI approved! Now I'll create the detailed implementation plan...",
                            "phase": "ui_approved",
                        })

                        # Ask Claude to create task plan
                        history = get_conversation_history(session_id)
                        await pool.send(session_id, {"type": "thinking"})

                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: conv.chat(
                                session_id,
                                history,
                                "Great, the UI is approved! Please create the detailed task plan now.",
                                phase="ui_approved",
                            ),
                        )
                        reply = result["reply"]
                        signals = result["signals"]
                        add_message(session_id, "assistant", reply)

                        await pool.send(session_id, {
                            "type": "message",
                            "role": "assistant",
                            "content": reply,
                            "phase": "ui_approved",
                        })

                        if signals.get("task_plan"):
                            plan = signals["task_plan"]
                            update_session(
                                session_id,
                                task_plan=json.dumps(plan),
                                phase="awaiting_task_approval",
                            )
                            pending_approval["tasks"] = plan
                            await pool.send(session_id, {
                                "type": "approval_request",
                                "approval_type": "tasks",
                                "approval_id": "task_plan",
                                "title": f"Implementation Plan ({len(plan)} tasks)",
                                "description": "Approve to start building your project.",
                                "data": plan,
                            })

                    elif approval_id == "task_plan":
                        update_session(session_id, phase="executing")
                        session = get_session(session_id)

                        req_str   = session.get("requirements", "{}")
                        plan_str  = session.get("task_plan", "[]")
                        requirements = json.loads(req_str) if req_str else {}
                        task_plan    = json.loads(plan_str) if plan_str else []

                        if not task_plan:
                            await pool.send(session_id, {
                                "type": "message",
                                "role": "assistant",
                                "content": "No task plan found. Please restart the planning phase.",
                                "phase": "error",
                            })
                            continue

                        # Create project directory
                        proj_name = requirements.get("project_name", "project").lower().replace(" ", "_")
                        proj_dir  = str(SESSIONS_DIR / session_id / proj_name)
                        Path(proj_dir).mkdir(parents=True, exist_ok=True)

                        code_analysis = session.get("tech_stack", "")
                        completed: list[str] = []

                        await pool.send(session_id, {
                            "type": "message",
                            "role": "assistant",
                            "content": f"🚀 Starting implementation! Building `{proj_name}` ({len(task_plan)} tasks)\n\nAll files will be created in:\n`{proj_dir}`",
                            "phase": "executing",
                        })

                        for i, task in enumerate(task_plan):
                            await pool.send(session_id, {
                                "type": "task_start",
                                "task_id": task["id"],
                                "title": task["title"],
                                "index": i,
                                "total": len(task_plan),
                            })

                            context_doc = conv.generate_context_document(
                                requirements=requirements,
                                task_plan=task_plan,
                                task_index=i,
                                project_dir=proj_dir,
                                code_analysis=code_analysis,
                                completed_tasks=completed,
                            )

                            async for event in stream_task(context_doc, proj_dir, task):
                                await pool.send(session_id, {
                                    "type": "task_log",
                                    "task_id": task["id"],
                                    **event,
                                })

                            completed.append(task["id"])
                            await pool.send(session_id, {
                                "type": "task_done",
                                "task_id": task["id"],
                                "index": i,
                                "total": len(task_plan),
                            })

                        update_session(session_id, phase="complete")
                        await pool.send(session_id, {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                f"🎉 **All {len(task_plan)} tasks complete!**\n\n"
                                f"Your project is ready at:\n`{proj_dir}`\n\n"
                                f"To run it: check the README.md that was generated inside the project folder."
                            ),
                            "phase": "complete",
                        })
                        await pool.send(session_id, {
                            "type": "phase_change",
                            "phase": "complete",
                            "project_dir": proj_dir,
                        })

    except WebSocketDisconnect:
        pool.remove(session_id)
