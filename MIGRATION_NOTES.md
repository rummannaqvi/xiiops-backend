# XiiOps Merged Backend — Migration Notes

## What Changed & Why

### 1. AI Backend: OpenAI → Gemini (the main goal)

| File | Before | After |
|------|--------|-------|
| `main.py` | `from langchain_openai import ChatOpenAI` | `from langchain_google_genai import ChatGoogleGenerativeAI` |
| `main.py` | `llm = ChatOpenAI(model="gpt-4o", ...)` | `llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", ...)` |
| `requirements.txt` | `langchain-openai` | `langchain-google-genai`, `google-generativeai` |
| `.env` | `OPENAI_API_KEY=...` | `GOOGLE_API_KEY=...` |

All three places in `main.py` that instantiated an LLM have been updated:
- The global `llm` used by `/api/v1/analyze` and `/api/v1/generate-infra`
- The `agent_llm` inside `/api/v1/generate-build`'s streamer
- The agent executor used by the `/api/chat` endpoint

Project B was already using `ChatGoogleGenerativeAI` — no change needed there.

---

### 2. Project Structure: Two Repos → One

All files now live in a single folder. Nothing was deleted — every feature from both projects is preserved.

**Files from Project A (kept as-is):**
- `docker_ops.py` — Docker build/push
- `infra_ops.py` — Terraform helpers
- `ssh_ops.py` — SSH deploy
- `utils.py` — Git clone, file tree

**Files from Project B (kept as-is):**
- `tools.py` — LangChain tools (GitHub API, Terraform, file I/O)
- `database.py` — PostgreSQL init (now gracefully skips if `DATABASE_URL` is unset)

**New/merged:**
- `main.py` — All routes from both projects in one FastAPI app
- `requirements.txt` — Unified dependencies
- `docker-compose.yml` — PostgreSQL + backend service
- `Dockerfile` — Single image for the merged backend
- `.env.example` — All required environment variables

---

### 3. API Routes (all preserved)

**From Project A:**
- `GET  /` — Health check
- `POST /api/v1/analyze` — Repo analysis
- `POST /api/v1/generate-build` — Agentic Docker build
- `POST /api/v1/generate-infra` — Terraform provisioning
- `POST /api/v1/deploy` — SSH deploy
- `POST /api/v1/destroy-infra` — Terraform destroy
- `POST /api/v1/metrics` — CloudWatch metrics
- `POST /api/v1/runtime-logs` — Docker logs via SSH
- `POST /api/v1/export-config` — ZIP config export
- `POST /api/v1/push-config` — Push config to GitHub branch
- `POST /api/v1/webhook` — GitHub webhook for CI/CD
- `POST /api/v1/pipeline-status` — CI/CD pipeline status
- `GET  /api/v1/project/status` — Registry lookup

**From Project B:**
- `GET  /health` — Health check
- `POST /api/chat` — Conversational AI agent
- `GET  /api/history/{session_id}` — Chat history

---

### 4. Database: Optional, not required

`database.py` now checks if `DATABASE_URL` is set before connecting. If it's not set, chat history falls back to in-memory (no persistence across restarts, but the server won't crash).

---

### 5. One bug fixed in `run_cicd_pipeline`

Project A's CI/CD pipeline function referenced `payload` and `use_nocache` variables that didn't exist in that scope (they were from the HTTP handler). These have been corrected to use the local variables `docker_user`, `docker_token` that are already extracted from the registry.

---

## How to Run

```bash
# 1. Copy env file
cp .env.example .env
# Edit .env and add your GOOGLE_API_KEY and GITHUB_TOKEN

# 2. Option A: Docker Compose (recommended, includes PostgreSQL)
docker-compose up --build

# 3. Option B: Local dev
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
