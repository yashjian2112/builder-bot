"""
FastAPI server — handles WebSocket chat, REST API, file serving.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Query, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from typing import List

import re

from core.memory import (
    init_db, create_session, get_session, list_sessions,
    update_session, add_message, get_conversation_history,
    delete_session, get_messages,
    create_user, get_user_by_token, verify_login, list_users,
    update_user_role, delete_user, user_count,
)

GITHUB_URL_RE = re.compile(r'https?://github\.com/[\w.\-]+/[\w.\-]+', re.IGNORECASE)
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

app = FastAPI(title="SMXDrives")

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

# ─── Auth helpers ────────────────────────────────────────

security = HTTPBearer(auto_error=False)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials if credentials else None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

async def require_admin(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ─── REST endpoints ──────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/api/auth/status")
async def auth_status():
    """Returns whether any users exist (to show setup vs login screen)."""
    return {"has_users": user_count() > 0}


@app.get("/api/auth/reset")
async def auth_reset(secret: str = ""):
    """Delete all users so setup can run again. Requires RESET_SECRET env var to match."""
    expected = os.environ.get("RESET_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing reset secret")
    from core.memory import _conn
    with _conn() as cur:
        cur.execute("DELETE FROM users")
    return {"ok": True, "message": "All users deleted — visit the app to create a new admin account"}


@app.post("/api/auth/login")
async def auth_login(body: dict):
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    user = verify_login(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"token": user["token"], "username": user["username"], "role": user["role"]}


@app.post("/api/auth/setup")
async def auth_setup(body: dict):
    """Create first admin user. Only works when no users exist."""
    if user_count() > 0:
        raise HTTPException(status_code=403, detail="Setup already complete")
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    user = create_user(username, password, role="admin")
    if not user:
        raise HTTPException(status_code=400, detail="Username already taken")
    return {"token": user["token"], "username": user["username"], "role": user["role"]}


@app.get("/api/users")
async def api_list_users(admin=Depends(require_admin)):
    return list_users()


@app.post("/api/users")
async def api_create_user(body: dict, admin=Depends(require_admin)):
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "member")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    if role not in ("admin", "member"):
        role = "member"
    user = create_user(username, password, role=role)
    if not user:
        raise HTTPException(status_code=400, detail="Username already taken")
    return {"username": user["username"], "role": user["role"]}


@app.patch("/api/users/{username}/role")
async def api_update_role(username: str, body: dict, admin=Depends(require_admin)):
    role = body.get("role", "member")
    if role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="Role must be admin or member")
    update_user_role(username, role)
    return {"ok": True}


@app.delete("/api/users/{username}")
async def api_delete_user(username: str, admin=Depends(require_admin)):
    if username == admin["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    delete_user(username)
    return {"ok": True}


@app.get("/api/sessions")
async def api_list_sessions(current_user=Depends(get_current_user)):
    # Admins see all sessions, members see only their own
    uname = current_user["username"] if current_user["role"] != "admin" else ""
    sessions = list_sessions(username=uname)
    for s in sessions:
        msgs = get_messages(s["id"])
        s["message_count"] = len([m for m in msgs if m["role"] == "user"])
    return sessions


@app.post("/api/sessions")
async def api_create_session(current_user=Depends(get_current_user)):
    sid = create_session(username=current_user["username"])
    return {"session_id": sid}


@app.delete("/api/session/{sid}")
async def api_delete_session(sid: str, current_user=Depends(get_current_user)):
    delete_session(sid)
    return {"ok": True}


@app.post("/api/session/{sid}/name")
async def api_rename_session(sid: str, body: dict, current_user=Depends(get_current_user)):
    update_session(sid, name=body.get("name", ""))
    return {"ok": True}


@app.get("/api/session/{sid}")
async def api_get_session(sid: str, current_user=Depends(get_current_user)):
    s = get_session(sid)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    history = get_conversation_history(sid)
    return {**s, "history": history}


@app.post("/api/upload/{sid}")
async def api_upload_folder(sid: str, files: List[UploadFile] = File(...), paths: str = Form("")):
    """Receive uploaded folder files and store them for analysis."""
    import aiofiles
    upload_dir = DATA_DIR / "uploads" / sid
    upload_dir.mkdir(parents=True, exist_ok=True)

    path_list = [p.strip() for p in paths.split("||") if p.strip()]

    for i, f in enumerate(files):
        rel_path = path_list[i] if i < len(path_list) else f.filename
        # Strip the top-level folder name (it's the project root)
        parts = Path(rel_path).parts
        if len(parts) > 1:
            rel_path = str(Path(*parts[1:]))
        dest = upload_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = await f.read()
        async with aiofiles.open(str(dest), "wb") as out:
            await out.write(content)

    return {"upload_dir": str(upload_dir), "file_count": len(files)}


@app.get("/mockup/{sid}/{name}")
async def serve_mockup(sid: str, name: str):
    path = MOCKUP_DIR / sid / name
    if not path.exists():
        return HTMLResponse("<h1>Mockup not found</h1>", status_code=404)
    return FileResponse(str(path), media_type="text/html")


# ─── WebSocket ───────────────────────────────────────────

@app.get("/api/auth/me")
async def auth_me(current_user=Depends(get_current_user)):
    return {"username": current_user["username"], "role": current_user["role"]}


@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str, token: str = Query(default=""), username: str = Query(default="")):
    await pool.add(session_id, websocket)

    # Resolve username from token if provided
    if token:
        user_obj = get_user_by_token(token)
        if user_obj:
            username = user_obj["username"]

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
        session_id = create_session(username=username)
        session = get_session(session_id)
    elif username and not session.get("username"):
        # Attach username to session if not already set
        update_session(session_id, username=username)

    history = get_conversation_history(session_id)
    if not history:
        welcome = (
            "Hey! I'm **SMXDrives AI** — your autonomous project engineer.\n\n"
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

                # Auto-detect GitHub URL typed in chat → silently route to folder analysis
                gh_match = GITHUB_URL_RE.search(user_text)
                if gh_match:
                    github_url = gh_match.group(0).rstrip("/")
                    add_message(session_id, "user", user_text)
                    data = {"type": "set_folder", "path": github_url, "source": "github"}
                    msg_type = "set_folder"
                    # Fall through: handled by the set_folder block below

                if msg_type == "message":
                    # Normal message — not redirected
                    add_message(session_id, "user", user_text)
                    session = get_session(session_id)
                    phase = session.get("phase", "greeting")
                    history = get_conversation_history(session_id)

                    await pool.send(session_id, {"type": "stream_start", "phase": phase})

                    extra = ""
                    code_analysis = session.get("tech_stack", "")

                    # Stream response via queue (sync generator → async WS)
                    q: asyncio.Queue = asyncio.Queue()
                    loop = asyncio.get_event_loop()

                    def _run_stream():
                        try:
                            for kind, payload in conv.stream_chat(
                                history[:-1],
                                user_text,
                                extra_context=extra,
                                phase=phase,
                            ):
                                loop.call_soon_threadsafe(q.put_nowait, (kind, payload))
                        except Exception as exc:
                            loop.call_soon_threadsafe(q.put_nowait, ("error", str(exc)))

                    threading.Thread(target=_run_stream, daemon=True).start()

                    reply = ""
                    signals = {}
                    while True:
                        kind, payload = await q.get()
                        if kind == "chunk":
                            await pool.send(session_id, {"type": "stream_chunk", "content": payload})
                        elif kind == "done":
                            reply = payload["reply"]
                            signals = payload["signals"]
                            break
                        elif kind == "error":
                            await pool.send(session_id, {"type": "stream_chunk", "content": f"\n\n[Error: {payload}]"})
                            break

                    add_message(session_id, "assistant", reply)
                    await pool.send(session_id, {
                        "type": "stream_end",
                        "phase": phase,
                    })

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

            # ── Folder path provided (or redirected from message) ──────
            if msg_type == "set_folder":
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
                        (
                            "The codebase has been fully analyzed — all file contents are in your context above. "
                            "You have complete access to the code. "
                            "Please briefly summarize what you found (tech stack, structure, key files), "
                            "then ask me what improvements or changes I want to make."
                        ),
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
            if msg_type == "approval":
                approval_id = data.get("approval_id")
                decision    = data.get("decision")   # "approve" or "reject"
                feedback    = data.get("feedback", "")

                session = get_session(session_id)

                if decision == "reject":
                    # Feed feedback back into conversation
                    reject_msg = feedback or "Please revise this."
                    add_message(session_id, "user", reject_msg)
                    history = get_conversation_history(session_id)
                    rej_phase = session.get("phase", "")

                    await pool.send(session_id, {"type": "stream_start", "phase": rej_phase})

                    q2: asyncio.Queue = asyncio.Queue()
                    loop2 = asyncio.get_event_loop()

                    def _run_reject_stream():
                        try:
                            for kind, payload in conv.stream_chat(
                                history[:-1],
                                reject_msg,
                                phase=rej_phase,
                            ):
                                loop2.call_soon_threadsafe(q2.put_nowait, (kind, payload))
                        except Exception as exc:
                            loop2.call_soon_threadsafe(q2.put_nowait, ("error", str(exc)))

                    threading.Thread(target=_run_reject_stream, daemon=True).start()

                    reply = ""
                    signals = {}
                    while True:
                        kind, payload = await q2.get()
                        if kind == "chunk":
                            await pool.send(session_id, {"type": "stream_chunk", "content": payload})
                        elif kind == "done":
                            reply = payload["reply"]
                            signals = payload["signals"]
                            break
                        elif kind == "error":
                            await pool.send(session_id, {"type": "stream_chunk", "content": f"\n\n[Error: {payload}]"})
                            break

                    add_message(session_id, "assistant", reply)
                    await pool.send(session_id, {"type": "stream_end", "phase": rej_phase})

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
                            "content": (
                                f"Starting implementation! Building `{proj_name}` ({len(task_plan)} tasks)\n\n"
                                f"All files will be created in:\n`{proj_dir}`"
                            ),
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
                                f"**All {len(task_plan)} tasks complete!**\n\n"
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
