# AI Tools Platform

A FastAPI web app that puts production automation tools behind a conversational Claude AI interface. Non-technical users log in, describe what they need in plain English, and Claude reads the appropriate directive, calls the right scripts, and streams the result back in real time.

Built for operations teams who need access to powerful automation without writing code.

---

## How It Works

```
Browser (team member)
    |
    | HTTPS + JWT cookie
    v
FastAPI app (genesys_tools_web.py)
    |
    | reads directive, streams via SSE
    v
Claude AI (claude-opus-4-7)
    |
    | subprocess calls
    v
Execution scripts (Python)
    |
    | API calls
    v
External systems (Genesys Cloud, Google Sheets, etc.)
```

1. User logs in with email + password (bcrypt, JWT session cookie)
2. User selects a tool from the catalog and describes the task
3. Claude reads the tool's directive (SOP), calls the execution scripts in order
4. Results stream back to the browser via Server-Sent Events
5. Every run is logged: who ran it, which scripts were called, duration, output snippet

---

## Key Features

- **Catalog-driven**: Tools are defined in `tools_catalog.json`. Adding a new tool means writing a directive and registering it in the catalog. No app code changes needed.
- **JWT auth**: `httpOnly` cookie, never readable by JavaScript. Passwords stored as bcrypt hashes only.
- **RBAC**: Admin and user roles. Admins can manage users and view run logs.
- **Streaming**: Claude responses stream token-by-token via SSE so long-running tools don't feel hung.
- **Execution logging**: Every tool run records the user, start/end time, duration, scripts called, and output snippets. Stored in SQLite on a Docker volume.
- **Discord notifications**: Tool completions and errors post to a configured Discord webhook.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Web framework | FastAPI + Jinja2 |
| AI orchestration | Anthropic Claude API |
| Auth | bcrypt + PyJWT + itsdangerous |
| Storage | SQLite on Docker volume |
| Streaming | Server-Sent Events (SSE) |
| Deployment | Docker on Linux VPS |

---

## Setup

### 1. Environment variables

```bash
cp .env.example .env
# Fill in: ANTHROPIC_API_KEY, GENESYS_CLIENT_ID, GENESYS_CLIENT_SECRET,
#          GENESYS_REGION, GMAIL_USER, GMAIL_APP_PASSWORD, DISCORD_WEBHOOK_URL
```

### 2. Build and run

```bash
docker build -f Dockerfile.genesys_tools -t ai-tools-platform:latest .
docker run -d \
  --name ai-tools-platform \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v ai-tools-data:/data \
  --env-file .env \
  ai-tools-platform:latest
```

### 3. Create the first admin user

```bash
docker exec -it ai-tools-platform python genesys_tools_web.py --seed-admin you@example.com
```

---

## Adding a Tool

1. Write a directive in `directives/your_tool.md` - purpose, inputs, steps, error handling
2. Add an entry to `tools_catalog.json`:
   ```json
   {
     "id": "your_tool",
     "label": "Your Tool Name",
     "directive": "directives/your_tool.md",
     "scripts": ["execution/your_tool.py"]
   }
   ```
3. Restart the container. The tool appears in the UI immediately.

---

## Security

- No secrets are ever returned in HTTP responses, page source, or logs
- Script execution is restricted to the allowlist in `tools_catalog.json`
- All unhandled errors return a generic message; details go to server logs only
- Admin role is checked server-side on every protected route
