#!/usr/bin/env python3
"""
Genesys Tools Platform - Hosted web application for the Enterprise Client automation toolkit.

Team members visit this app in a browser, authenticate with their email and password,
and interact with tools through a conversational Claude-powered chat interface.
The underlying execution scripts run unchanged as subprocesses on the server.

Deployment (Docker on Hostinger):
  docker compose up -d genesys-tools

First-time admin setup (run inside the container):
  docker exec -it genesys-tools python /app/execution/genesys_tools_web.py --seed-admin you@company.com

Security guarantees:
  - No secrets in any HTTP response, URL, or page source
  - Passwords stored as bcrypt hashes only
  - Sessions via httpOnly JWT cookie (inaccessible to JavaScript)
  - Admin role checked server-side on every request
  - Script execution restricted to allowlist in tools_catalog.json
  - All unhandled errors return a generic message; details go to server logs only
"""
import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

# ---------------------------------------------------------------------------
# Paths (relative to repo root, works whether running locally or in Docker)
# ---------------------------------------------------------------------------

_root = Path(__file__).parent.parent   # repo root: /app when in Docker
_exec = _root / "execution"
_templates = _exec / "templates" / "cc_tools"
_directives = _root / "directives"
_catalog_path = _root / "tools_catalog.json"
_tmp = _root / ".tmp"

# ---------------------------------------------------------------------------
# Database (SQLite on Docker volume at /data/users.db)
# ---------------------------------------------------------------------------

DB_PATH = "/data/users.db"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    os.makedirs("/data", exist_ok=True)
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL UNIQUE,
                password_hash TEXT,
                is_admin    INTEGER NOT NULL DEFAULT 0,
                status      TEXT NOT NULL DEFAULT 'pending',
                force_reset INTEGER NOT NULL DEFAULT 1,
                created_at  REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                messages   TEXT NOT NULL DEFAULT '[]',
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS run_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                user_email   TEXT NOT NULL,
                tool_id      TEXT NOT NULL,
                tool_label   TEXT NOT NULL,
                started_at   REAL NOT NULL,
                ended_at     REAL,
                duration_s   REAL,
                script_calls TEXT NOT NULL DEFAULT '[]',
                error        TEXT,
                status       TEXT NOT NULL DEFAULT 'running'
            )
        """)
        conn.commit()


def _log_start(session_id: str, user_email: str, tool_id: str, tool_label: str) -> int:
    """Create a run_log entry and return its id."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO run_logs (session_id, user_email, tool_id, tool_label, started_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (session_id, user_email, tool_id, tool_label, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def _log_script_call(log_id: int, script: str, args: list, duration_s: float, success: bool, output_snippet: str) -> None:
    """Append a script call record to the run_log entry."""
    with _db() as conn:
        row = conn.execute("SELECT script_calls FROM run_logs WHERE id = ?", (log_id,)).fetchone()
        if not row:
            return
        calls = json.loads(row["script_calls"])
        calls.append({
            "script": script,
            "args": args,
            "duration_s": round(duration_s, 2),
            "success": success,
            "output_snippet": output_snippet[:300],
        })
        conn.execute("UPDATE run_logs SET script_calls = ? WHERE id = ?", (json.dumps(calls), log_id))
        conn.commit()


def _log_end(log_id: int, status: str, error: Optional[str] = None) -> None:
    """Mark a run_log entry as complete."""
    ended = time.time()
    row_data = None
    with _db() as conn:
        row = conn.execute("SELECT started_at, user_email, tool_label, script_calls FROM run_logs WHERE id = ?", (log_id,)).fetchone()
        duration = round(ended - row["started_at"], 2) if row else None
        conn.execute(
            "UPDATE run_logs SET ended_at = ?, duration_s = ?, status = ?, error = ? WHERE id = ?",
            (ended, duration, status, error, log_id),
        )
        conn.commit()
        if row:
            had_scripts = json.loads(row["script_calls"] or "[]")
            if had_scripts or (status == "error" and error):
                row_data = (row["user_email"], row["tool_label"], duration)
    if row_data is not None:
        _discord_notify(status, row_data, error)


def _discord_notify(status: str, row_data, error: Optional[str]) -> None:
    """Post a run summary to the Discord webhook. Fire-and-forget; never raises."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        import urllib.request as _urllib_request
        if row_data:
            user_email, tool_label, duration_s = row_data
            dur = f"{duration_s:.0f}s" if duration_s is not None else "?"
        else:
            user_email, tool_label, dur = "unknown", "unknown", "?"

        if status == "complete":
            icon = "\u2705"
            title = f"{icon} Run complete"
        else:
            icon = "\u274c"
            title = f"{icon} Run error"

        lines = [
            f"**{title}**",
            f"User: {user_email}",
            f"Tool: {tool_label}",
            f"Duration: {dur}",
        ]
        if error:
            lines.append(f"Error: {error[:300]}")

        payload = json.dumps({"content": "\n".join(lines)}).encode()
        req = _urllib_request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "genesys-tools/1.0"},
            method="POST",
        )
        _urllib_request.urlopen(req, timeout=5)
    except Exception:
        pass


def _load_session(session_id: str) -> list:
    """Load conversation history from DB. Returns empty list if not found."""
    with _db() as conn:
        row = conn.execute(
            "SELECT messages FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return json.loads(row["messages"]) if row else []


def _serialize_block(block) -> dict:
    """
    Serialize an Anthropic SDK content block to only the fields the API accepts.
    model_dump() includes internal SDK fields (e.g. parsed_output) that the API rejects.
    """
    if isinstance(block, dict):
        return block
    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {"type": "text", "text": block.text}
    if block_type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if block_type == "tool_result":
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
    # Fallback: try model_dump but strip known-bad internal fields
    try:
        d = block.model_dump()
        d.pop("parsed_output", None)
        return d
    except AttributeError:
        return {"type": str(block_type)}


def _save_session(session_id: str, messages: list) -> None:
    """Persist conversation history to DB. Serializes Anthropic content blocks to API-safe dicts."""
    serializable = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            serializable.append({"role": msg["role"], "content": content})
        elif isinstance(content, list):
            serializable.append({
                "role": msg["role"],
                "content": [_serialize_block(b) for b in content],
            })
        else:
            serializable.append({"role": msg["role"], "content": str(content)})
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, messages, updated_at) VALUES (?, ?, ?)",
            (session_id, json.dumps(serializable), time.time()),
        )


def _get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()


def _get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with _db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _get_pending_users() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()


def _get_all_users() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()


# ---------------------------------------------------------------------------
# Tools catalog
# ---------------------------------------------------------------------------

def _load_catalog() -> dict:
    with open(str(_catalog_path)) as f:
        return json.load(f)


def _allowed_scripts_for_tool(tool_id: str) -> set[str]:
    """Return the set of script paths permitted for a given tool_id."""
    catalog = _load_catalog()
    for tool in catalog["tools"]:
        if tool["id"] == tool_id:
            return set(tool["scripts"])
    return set()


def _get_tool(tool_id: str) -> Optional[dict]:
    catalog = _load_catalog()
    for tool in catalog["tools"]:
        if tool["id"] == tool_id:
            return tool
    return None


# ---------------------------------------------------------------------------
# Script execution (security: allowlist enforced, no secrets in output)
# ---------------------------------------------------------------------------

def _run_script(script: str, args: list[str], allowed_scripts: set[str]) -> str:
    """
    Run an approved execution script and return its combined stdout+stderr.
    Raises ValueError if the script is not in the allowlist.
    Never exposes environment variables or internal paths in the return value
    beyond what the script itself outputs.
    """
    if script not in allowed_scripts:
        raise ValueError(f"Script '{script}' is not approved for this tool")

    script_path = str(_root / script)
    if not os.path.exists(script_path):
        return f"[ERROR] Script not found: {script}"

    try:
        result = subprocess.run(
            [sys.executable, script_path] + args,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(_root),
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "[ERROR] Script timed out after 5 minutes"
    except Exception:
        # Log internally; return a generic message to Claude/user
        traceback.print_exc()
        return "[ERROR] Script encountered an unexpected error. Details have been logged."


# ---------------------------------------------------------------------------
# Claude tool schema
# ---------------------------------------------------------------------------

_RUN_SCRIPT_TOOL = {
    "name": "run_script",
    "description": (
        "Run one of the approved execution scripts after gathering all required inputs "
        "from the user. Only call this after you have confirmed the user's intent and "
        "all necessary parameters. For destructive actions (reboots, provisioning), "
        "always confirm with the user in the chat before calling this."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Script path, e.g. execution/list_genesys_phones.py",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command-line arguments to pass to the script",
            },
        },
        "required": ["script", "args"],
    },
}


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def _build_system_prompt(tool: dict, user_name: str) -> str:
    from datetime import date
    today = date.today().isoformat()

    directive_path = str(_root / tool["directive"])
    try:
        with open(directive_path) as f:
            directive_text = f.read()
    except Exception:
        directive_text = "(directive not available)"

    allowed = ", ".join(tool["scripts"])

    return f"""You are an assistant on the Genesys Tools platform for Enterprise Client. \
You are helping {user_name} use the following tool: {tool['label']}.

Today's date is {today}. Use this when interpreting relative date references like "last week", "yesterday", or "this month".

You have access to one tool: run_script. You may ONLY call scripts from this list:
{allowed}

## Directive (your instructions for this tool):

{directive_text}

## Platform behavior rules:

1. Be fast. Minimize the number of messages. Collect all inputs for a step in one message, \
   not one question at a time.
2. Never ask for information you can look up yourself by running a script.
3. Before any action that creates, modifies, or reboots something, confirm with the user. \
   Include the confirmation question in the same message as the summary -- do not send the \
   summary and then wait for the user to ask "go ahead?"
4. When a script produces output, summarize it in plain English. Do not dump raw JSON.
5. If a script fails, explain the error in plain terms and suggest next steps. \
   Do not expose stack traces or file paths in your response.
6. Keep responses concise. Users are IT staff, not developers.
7. When the task is fully complete with nothing left to do or ask, end your final \
   message with exactly this token on its own line: [SESSION_COMPLETE]

Note: For phone reboots and other destructive actions, user confirmation in this chat \
IS the human-in-the-loop approval. After the user confirms in chat, you may pass \
--force to the reboot script."""


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

def _create_fastapi_app():
    from fastapi import FastAPI, Request, Form, Depends, Cookie, HTTPException
    from fastapi.responses import (
        HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
    )
    from fastapi.templating import Jinja2Templates

    from cc_tools_auth import (
        hash_password, verify_password, create_jwt, decode_jwt,
        generate_temp_password, generate_reset_token, verify_reset_token,
    )
    from cc_tools_email import (
        send_admin_notification, send_temp_password, send_reset_link,
        send_feedback_notification,
    )
    from sheets_log import append_run, ensure_header_row

    templates = Jinja2Templates(directory=str(_templates))

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        _init_db()
        _tmp.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(_exec))
        ensure_header_row()
        yield

    fastapi_app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    # ---- Security headers middleware ----------------------------------------

    @fastapi_app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline';"
        )
        return response

    # ---- Global error handler -----------------------------------------------

    @fastapi_app.exception_handler(Exception)
    async def global_error_handler(request: Request, exc: Exception):
        # Full traceback goes to server logs (developer-visible via docker logs)
        print(f"[ERROR] {request.url.path}: {traceback.format_exc()}")
        # Generic message returned to the browser - no internal details
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"error": "Something went wrong. Please try again."},
                status_code=500,
            )
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "Something went wrong. Please try again."},
            status_code=500,
        )

    # ---- Auth helpers -------------------------------------------------------

    def _current_user_from_cookie(cc_session: Optional[str] = Cookie(default=None)):
        if not cc_session:
            return None
        payload = decode_jwt(cc_session)
        if not payload:
            return None
        return payload

    def _require_auth(cc_session: Optional[str] = Cookie(default=None)):
        payload = _current_user_from_cookie(cc_session)
        if not payload:
            raise HTTPException(status_code=302, headers={"Location": "/login"})
        if payload.get("force_reset"):
            raise HTTPException(status_code=302, headers={"Location": "/set-password"})
        return payload

    def _require_admin(cc_session: Optional[str] = Cookie(default=None)):
        payload = _current_user_from_cookie(cc_session)
        if not payload or not payload.get("admin"):
            raise HTTPException(status_code=302, headers={"Location": "/"})
        return payload

    def _set_session_cookie(response, user_id: int, email: str, is_admin: bool):
        token = create_jwt(user_id, email, is_admin, force_reset=False)
        response.set_cookie(
            key="cc_session",
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=86400,
        )

    # ---- Public routes ------------------------------------------------------

    @fastapi_app.get("/login", response_class=HTMLResponse)
    async def get_login(request: Request, cc_session: Optional[str] = Cookie(default=None)):
        if _current_user_from_cookie(cc_session):
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @fastapi_app.post("/login", response_class=HTMLResponse)
    async def post_login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
    ):
        user = _get_user_by_email(email)
        if not user or user["status"] != "active" or not user["password_hash"]:
            # Same response whether email exists or not (prevents enumeration)
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid email or password.", "email": email},
            )
        if not verify_password(password, user["password_hash"]):
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid email or password.", "email": email},
            )
        force_reset = bool(user["force_reset"])
        response = RedirectResponse("/set-password" if force_reset else "/", status_code=302)
        token = create_jwt(user["id"], user["email"], bool(user["is_admin"]), force_reset=force_reset)
        response.set_cookie(
            key="cc_session",
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=86400,
        )
        return response

    @fastapi_app.get("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=302)
        response.delete_cookie("cc_session")
        return response

    @fastapi_app.get("/register", response_class=HTMLResponse)
    async def get_register(request: Request):
        return templates.TemplateResponse(request, "register.html", {"submitted": False})

    @fastapi_app.post("/register", response_class=HTMLResponse)
    async def post_register(
        request: Request,
        name: str = Form(...),
        email: str = Form(...),
    ):
        email = email.lower().strip()
        name = name.strip()
        if not name or not email or "@" not in email:
            return templates.TemplateResponse(
                request, "register.html",
                {"submitted": False, "error": "Please enter a valid name and email."},
            )
        existing = _get_user_by_email(email)
        # Always show success to prevent email enumeration
        if not existing:
            with _db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO users (name, email, status, force_reset, created_at) "
                    "VALUES (?, ?, 'pending', 1, ?)",
                    (name, email, time.time()),
                )
                conn.commit()
            admin_email = os.environ.get("ADMIN_EMAIL", "")
            app_url = os.environ.get("APP_URL", "")
            if admin_email:
                send_admin_notification(admin_email, name, email, app_url)
        return templates.TemplateResponse(request, "register.html", {"submitted": True})

    @fastapi_app.get("/set-password", response_class=HTMLResponse)
    async def get_set_password(
        request: Request,
        cc_session: Optional[str] = Cookie(default=None),
    ):
        payload = decode_jwt(cc_session) if cc_session else None
        if not payload or not payload.get("force_reset"):
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse(request, "set_password.html", {"error": None})

    @fastapi_app.post("/set-password", response_class=HTMLResponse)
    async def post_set_password(
        request: Request,
        password: str = Form(...),
        confirm: str = Form(...),
        cc_session: Optional[str] = Cookie(default=None),
    ):
        payload = decode_jwt(cc_session) if cc_session else None
        if not payload or not payload.get("force_reset"):
            return RedirectResponse("/login", status_code=302)

        if password != confirm:
            return templates.TemplateResponse(
                request, "set_password.html", {"error": "Passwords do not match."},
            )
        if len(password) < 12:
            return templates.TemplateResponse(
                request, "set_password.html", {"error": "Password must be at least 12 characters."},
            )

        user = _get_user_by_id(int(payload["sub"]))
        if not user:
            return RedirectResponse("/login", status_code=302)

        # Verify they're not reusing the temp password
        if user["password_hash"] and verify_password(password, user["password_hash"]):
            return templates.TemplateResponse(
                request, "set_password.html", {"error": "Please choose a different password."},
            )

        new_hash = hash_password(password)
        with _db() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, force_reset = 0 WHERE id = ?",
                (new_hash, user["id"]),
            )
            conn.commit()

        response = RedirectResponse("/", status_code=302)
        _set_session_cookie(response, user["id"], user["email"], bool(user["is_admin"]))
        return response

    @fastapi_app.get("/forgot", response_class=HTMLResponse)
    async def get_forgot(request: Request):
        return templates.TemplateResponse(request, "forgot.html", {"submitted": False})

    @fastapi_app.post("/forgot", response_class=HTMLResponse)
    async def post_forgot(request: Request, email: str = Form(...)):
        email = email.lower().strip()
        # Always show the same response (prevents user enumeration)
        user = _get_user_by_email(email)
        if user and user["status"] == "active":
            token = generate_reset_token(email)
            app_url = os.environ.get("APP_URL", "")
            reset_url = f"{app_url}/reset?token={token}"
            send_reset_link(email, reset_url)
        return templates.TemplateResponse(request, "forgot.html", {"submitted": True})

    @fastapi_app.get("/reset", response_class=HTMLResponse)
    async def get_reset(request: Request, token: str = ""):
        email = verify_reset_token(token)
        if not email:
            return templates.TemplateResponse(
                request, "reset.html", {"valid": False, "token": token},
            )
        return templates.TemplateResponse(
            request, "reset.html", {"valid": True, "token": token, "error": None},
        )

    @fastapi_app.post("/reset", response_class=HTMLResponse)
    async def post_reset(
        request: Request,
        token: str = Form(...),
        password: str = Form(...),
        confirm: str = Form(...),
    ):
        email = verify_reset_token(token)
        if not email:
            return templates.TemplateResponse(
                request, "reset.html", {"valid": False, "token": token},
            )
        if password != confirm:
            return templates.TemplateResponse(
                request, "reset.html",
                {"valid": True, "token": token, "error": "Passwords do not match."},
            )
        if len(password) < 12:
            return templates.TemplateResponse(
                request, "reset.html",
                {"valid": True, "token": token, "error": "Password must be at least 12 characters."},
            )
        user = _get_user_by_email(email)
        if not user:
            return RedirectResponse("/login", status_code=302)

        new_hash = hash_password(password)
        with _db() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, force_reset = 0 WHERE id = ?",
                (new_hash, user["id"]),
            )
            conn.commit()

        return RedirectResponse("/login?reset=1", status_code=302)

    # ---- Protected routes ---------------------------------------------------

    def _load_changelog() -> list:
        try:
            changelog_path = _root / "changelog.json"
            with open(changelog_path) as f:
                return json.load(f).get("entries", [])
        except Exception:
            return []

    @fastapi_app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, session=Depends(_require_auth)):
        catalog = _load_catalog()
        categories: dict[str, list] = {}
        for tool in catalog["tools"]:
            cat = tool["category"]
            categories.setdefault(cat, []).append(tool)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "user_name": session["email"].split("@")[0].title(),
                "user_email": session["email"],
                "is_admin": session.get("admin", False),
                "categories": categories,
                "changelog": _load_changelog(),
            },
        )

    @fastapi_app.get("/tool/{tool_id}", response_class=HTMLResponse)
    async def tool_chat(request: Request, tool_id: str, session=Depends(_require_auth)):
        tool = _get_tool(tool_id)
        if not tool:
            return RedirectResponse("/", status_code=302)
        session_id = str(uuid.uuid4())
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "tool": tool,
                "session_id": session_id,
                "user_name": session["email"].split("@")[0].title(),
                "is_admin": session.get("admin", False),
            },
        )

    # ---- Dashboard feedback endpoint ----------------------------------------

    @fastapi_app.post("/api/feedback")
    async def api_dashboard_feedback(request: Request, session=Depends(_require_auth)):
        import asyncio as _asyncio
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid request body"}, status_code=400)
        message = body.get("message", "").strip()
        if not message:
            return JSONResponse({"error": "Empty feedback"}, status_code=400)
        user_email = session["email"]
        user_name = user_email.split("@")[0].title()
        app_url = os.environ.get("APP_URL", "")
        notify_emails: set[str] = {"justin@vonimate.com"}
        admin_email = os.environ.get("ADMIN_EMAIL", "")
        if admin_email:
            notify_emails.add(admin_email.lower())
        await _asyncio.to_thread(
            append_run,
            user_email=user_email,
            user_name=user_name,
            tool_id="dashboard",
            tool_label="Dashboard",
            inputs_summary="(feedback)",
            output_summary="",
            feedback=message,
        )
        for recipient in notify_emails:
            await _asyncio.to_thread(
                send_feedback_notification,
                recipient, user_name, user_email,
                "Dashboard", message, app_url,
            )
        return JSONResponse({"ok": True})

    # ---- Chat SSE endpoint --------------------------------------------------

    @fastapi_app.post("/api/chat")
    async def api_chat(request: Request, session=Depends(_require_auth)):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid request body"}, status_code=400)

        tool_id = body.get("tool_id", "")
        session_id = body.get("session_id", "")
        user_message = body.get("message", "").strip()
        is_feedback = body.get("is_feedback", False)

        tool = _get_tool(tool_id)
        if not tool:
            return JSONResponse({"error": "Unknown tool"}, status_code=404)
        if not user_message:
            return JSONResponse({"error": "Empty message"}, status_code=400)

        allowed_scripts = _allowed_scripts_for_tool(tool_id)
        user_name = session["email"].split("@")[0].title()
        user_email = session["email"]

        # Feedback bypasses Claude entirely - log it and notify admin, then done.
        if is_feedback:
            import asyncio as _asyncio

            async def feedback_stream() -> AsyncGenerator[str, None]:
                await _asyncio.to_thread(
                    append_run,
                    user_email=user_email,
                    user_name=user_name,
                    tool_id=tool_id,
                    tool_label=tool["label"],
                    inputs_summary="(feedback)",
                    output_summary="",
                    feedback=user_message,
                )
                app_url = os.environ.get("APP_URL", "")
                notify_emails = {"justin@vonimate.com"}
                admin_email = os.environ.get("ADMIN_EMAIL", "")
                if admin_email:
                    notify_emails.add(admin_email.lower())
                for recipient in notify_emails:
                    await _asyncio.to_thread(
                        send_feedback_notification,
                        recipient, user_name, user_email,
                        tool["label"], user_message, app_url,
                    )
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

            return StreamingResponse(
                feedback_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def stream() -> AsyncGenerator[str, None]:
            log_id = await asyncio.to_thread(
                _log_start, session_id, user_email, tool_id, tool["label"]
            )
            try:
                import anthropic as anthropic_sdk
                client = anthropic_sdk.AsyncAnthropic(
                    api_key=os.environ["ANTHROPIC_API_KEY"],
                    max_retries=4,
                )
                messages = _load_session(session_id)
                messages.append({"role": "user", "content": user_message})
                _save_session(session_id, messages)
                system_prompt = _build_system_prompt(tool, user_name)
            except Exception as _init_err:
                traceback.print_exc()
                await asyncio.to_thread(_log_end, log_id, "error", str(_init_err))
                yield f"data: {json.dumps({'type': 'error', 'content': f'Init error: {type(_init_err).__name__}: {_init_err}'})}\n\n"
                return

            # SDK handles 429/500/529 retries automatically (max_retries=4, exponential backoff).
            # If Opus is still overloaded after SDK retries, fall back to Sonnet.
            _model_sequence = ["claude-opus-4-6", "claude-sonnet-4-6"]

            while True:
                text_buffer = []
                final_message = None
                _last_err = None

                for _model in _model_sequence:
                    text_buffer = []
                    final_message = None
                    try:
                        async with client.messages.stream(
                            model=_model,
                            max_tokens=4096,
                            system=system_prompt,
                            tools=[_RUN_SCRIPT_TOOL],
                            messages=messages,
                        ) as stream_ctx:
                            async for chunk in stream_ctx.text_stream:
                                text_buffer.append(chunk)
                                yield f"data: {json.dumps({'type': 'text', 'content': chunk})}\n\n"

                            final_message = await stream_ctx.get_final_message()
                        break  # success - stop trying models
                    except Exception as _api_err:
                        _is_overloaded = "overloaded" in str(_api_err).lower()
                        if _is_overloaded and _model != _model_sequence[-1]:
                            print(f"[FALLBACK] {_model} overloaded, falling back to next model")
                            yield f"data: {json.dumps({'type': 'text', 'content': ' _(switching to fallback model...)_ '})}\n\n"
                            _last_err = _api_err
                            continue
                        traceback.print_exc()
                        err_msg = f"{type(_api_err).__name__}: {_api_err}"
                        await asyncio.to_thread(_log_end, log_id, "error", err_msg)
                        yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"
                        return

                if final_message is None:
                    await asyncio.to_thread(_log_end, log_id, "complete")
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Add assistant response to history and persist
                messages.append({"role": "assistant", "content": final_message.content})
                _save_session(session_id, messages)

                # Check for tool calls
                tool_calls = [b for b in final_message.content if b.type == "tool_use"]

                if final_message.stop_reason == "end_turn" or not tool_calls:
                    await asyncio.to_thread(_log_end, log_id, "complete")
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                # Execute tool calls
                tool_results = []
                for tool_use in tool_calls:
                    if tool_use.name != "run_script":
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps({"error": f"Unknown tool: {tool_use.name}"}),
                        })
                        continue

                    script = tool_use.input.get("script", "")
                    args = tool_use.input.get("args", [])

                    yield f"data: {json.dumps({'type': 'tool_start', 'script': script, 'args': args})}\n\n"

                    # Run in thread pool (subprocess is blocking)
                    _script_start = time.time()
                    try:
                        result = await asyncio.to_thread(
                            _run_script, script, args, allowed_scripts
                        )
                        _script_success = not result.startswith("[ERROR]")
                    except ValueError as exc:
                        result = f"[SECURITY] {exc}"
                        _script_success = False
                    _script_duration = time.time() - _script_start
                    await asyncio.to_thread(
                        _log_script_call, log_id, script, args,
                        _script_duration, _script_success, result
                    )

                    yield f"data: {json.dumps({'type': 'tool_result', 'content': result})}\n\n"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    })

                messages.append({"role": "user", "content": tool_results})
                _save_session(session_id, messages)
                # Loop continues to get Claude's response to the tool result

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- Admin routes -------------------------------------------------------

    @fastapi_app.get("/admin", response_class=HTMLResponse)
    async def admin_panel(request: Request, session=Depends(_require_admin)):
        pending = _get_pending_users()
        all_users = _get_all_users()
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "pending": [dict(u) for u in pending],
                "all_users": [dict(u) for u in all_users],
                "user_name": session["email"].split("@")[0].title(),
                "is_admin": True,
            },
        )

    @fastapi_app.post("/admin/approve")
    async def admin_approve(
        request: Request,
        session=Depends(_require_admin),
    ):
        body = await request.json()
        user_id = body.get("user_id")
        if not user_id:
            return JSONResponse({"error": "user_id required"}, status_code=400)

        user = _get_user_by_id(user_id)
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)
        if user["status"] != "pending":
            return JSONResponse({"error": "User is not pending"}, status_code=400)

        temp_pw = generate_temp_password()
        pw_hash = hash_password(temp_pw)

        with _db() as conn:
            conn.execute(
                "UPDATE users SET status = 'active', password_hash = ?, force_reset = 1 WHERE id = ?",
                (pw_hash, user_id),
            )
            conn.commit()

        app_url = os.environ.get("APP_URL", "")
        sent = send_temp_password(user["email"], user["name"], temp_pw, app_url)

        return JSONResponse({
            "ok": True,
            "email_sent": sent,
            "note": "Temp password was emailed to the user and is not stored here.",
        })

    @fastapi_app.post("/admin/deny")
    async def admin_deny(request: Request, session=Depends(_require_admin)):
        body = await request.json()
        user_id = body.get("user_id")
        if not user_id:
            return JSONResponse({"error": "user_id required"}, status_code=400)
        with _db() as conn:
            conn.execute("UPDATE users SET status = 'denied' WHERE id = ?", (user_id,))
            conn.commit()
        return JSONResponse({"ok": True})

    # ---- Reporting endpoint (admin only) ------------------------------------

    @fastapi_app.get("/api/report")
    async def api_report(
        request: Request,
        session=Depends(_require_admin),
        limit: int = 50,
    ):
        """
        Returns recent run logs with timing, script calls, and outcomes.
        Used by Claude to generate activity reports.
        """
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM run_logs ORDER BY started_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])

    return fastapi_app


# ---------------------------------------------------------------------------
# Admin seeding (run directly in the container)
#
# Usage:
#   docker exec -it genesys-tools \
#     python /app/execution/genesys_tools_web.py --seed-admin you@company.com
#
# Temp password is printed to stdout only - never to any URL or web response.
# ---------------------------------------------------------------------------

def _seed_admin(email: str) -> None:
    sys.path.insert(0, str(_exec))
    from cc_tools_auth import hash_password, generate_temp_password

    _init_db()
    email = email.lower().strip()
    existing = _get_user_by_email(email)
    if existing:
        print(f"[seed_admin] User {email} already exists (status={existing['status']}). No changes made.")
        return

    temp_pw = generate_temp_password()
    pw_hash = hash_password(temp_pw)

    with _db() as conn:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, is_admin, status, force_reset, created_at) "
            "VALUES (?, ?, ?, 1, 'active', 1, ?)",
            ("Admin", email, pw_hash, time.time()),
        )
        conn.commit()

    print(f"\n[seed_admin] Admin account created for {email}")
    print(f"[seed_admin] Temporary password: {temp_pw}")
    print(f"[seed_admin] Log in and change your password immediately.\n")
    print("This password is printed here only. It is not stored in plaintext anywhere.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

fastapi_app = _create_fastapi_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genesys Tools Platform")
    parser.add_argument("--seed-admin", metavar="EMAIL", help="Create the first admin user")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.seed_admin:
        _seed_admin(args.seed_admin)
    else:
        import uvicorn
        uvicorn.run(fastapi_app, host=args.host, port=args.port)

# revised

# revised

# rev 1

# rev 7

# rev 9

# rev 10

# rev 11
