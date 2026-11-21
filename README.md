# LLM Chatbot with Key Failover and Response Caching

A full-stack chatbot: **React** frontend, **FastAPI** backend and **LiteLLM** gateway. Features:

- multi-turn conversations, with history persisted in **SQLite** (`/data/app.db`)
- **ordered failover across multiple API keys** on 429, quota and 5xx errors
- **LiteLLM response caching**
- accurate per-request token usage and server-measured latency on every reply

```
┌──────────────┐   POST /chat     ┌──────────────────────────────────────────────┐
│ React (Vite) │ ───────────────▶ │ FastAPI                                      │
│ nginx :3000  │ ◀─────────────── │  routes ─▶ ChatService ─▶ FailoverLLMClient  │
└──────────────┘  reply+metadata  │              │                │             │
                                  │              ▼                ▼             │
                                  │        SQLite /data     litellm.acompletion │
                                  │   (sessions, messages)  + litellm.Cache     │
                                  └───────────────────────────────┬─────────────┘
                                                key_1 ─▶ key_2 ─▶ … (provider)
```

---

## Live URLs

| Tier | URL |
| --- | --- |
| Frontend | https://frontend-production-4f5ae195.up.railway.app |
| Backend | https://backend-production-42e9bc30.up.railway.app ([/health](https://backend-production-42e9bc30.up.railway.app/health)) |
| Repository | https://github.com/Vnagi08/llm-chatbot-failover-cache |
| Demo video | https://youtu.be/3oJ_vo_h1vo |

Both tiers are hosted on Railway. The backend uses a persistent Railway volume at `/data` and has `ENABLE_FAILURE_SIMULATION=true`. Model: `gemini/gemini-3.5-flash-lite`, with two Gemini keys from separate Google projects.

Verified on the live deployment:
- `/health` returns 200 with 2 keys configured and the cache enabled
- a live answer from key_1, and multi-turn context
- the same first message in a new session returned `LLM_CACHE` in 1 ms (versus about 630 ms live), with an identical reply and tokens
- `key_1=429` and `key_1=500` were answered genuinely by key_2 with `fallback_attempts: 1`
- `*=429` returned 503 and stored nothing (history returns 404)
- history survived a backend redeploy
- the browser UI works against the live backend

---

## Run locally: with Docker and without

### With Docker (recommended)

Requirements: Docker with Compose v2, and `make` (optional).

```bash
cp .env.example .env          # then put two real keys in LLM_API_KEYS
make up                       # = docker compose up -d --build
# Frontend: http://localhost:3000
# Backend:  http://localhost:8000   (http://localhost:8000/health)
make logs                     # follow logs
make down                     # stop (the SQLite volume is kept)
make clean                    # stop AND delete the data volume
```

Without `make` (for example on Windows), run `docker compose up -d --build` and `docker compose down` directly.

The backend stores SQLite in the named volume `llm-chatbot-data`, mounted at `/data`. History survives `docker compose restart` and `docker compose down` followed by `up`.

### Without Docker

```bash
# Backend (Python 3.10+)
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export LLM_API_KEYS="key1,key2" DATABASE_URL="sqlite:///./data/app.db" ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:app --reload --port 8000

# Frontend (Node 18+)
cd frontend
npm ci
VITE_API_BASE_URL=http://localhost:8000 npm run dev    # http://localhost:3000
```

### Tests

```bash
cd backend && python -m pytest                    # 57 tests, LLM mocked, no credits used
LIVE_LLM_API_KEYS="key1,key2" python -m pytest -m integration -v   # real provider
```

---

## Design decisions: key fallback, caching, SQLite session storage

### Code layout

```
backend/app/
  main.py                 app factory, lifespan, CORS, JSON error handlers
  config.py               pydantic-settings (env vars only)
  schemas.py              Pydantic request/response models (API contract)
  db.py                   SQLite schema + transactional repository
  api/routes.py           /health, /chat, /sessions/{id}/messages
  services/chat_service.py  history -> prompt -> LLM -> atomic save, per-session lock
  llm/client.py           FailoverLLMClient (ordered keys, attribution, tokens, latency)
  llm/errors.py           which errors trigger failover
  llm/cache.py            LiteLLM cache setup + cache-hit detection
  llm/simulation.py       X-Simulate-Failure parsing -> LiteLLM exception types
  logging_config.py       log filter that redacts API keys
frontend/src/             App.jsx (UI), api.js (backend client), styles.css
```

### Key fallback

- `LLM_API_KEYS=k1,k2,...` become `key_1`, `key_2`, … in that order. Each key uses `LLM_MODEL`; the optional `LLM_MODELS` can give each key its own model or provider.
- `FailoverLLMClient.complete()` tries keys **sequentially**. Every attempt is a real `litellm.acompletion(model, messages, api_key=<that key>, num_retries=0, timeout=…)`, so the key that produced a reply is always known exactly.
- LiteLLM maps provider errors to typed exceptions, and those decide whether to fail over:
  - **Next key:** `RateLimitError` (429) and quota or `RESOURCE_EXHAUSTED` errors, any 5xx (`InternalServerError`, `BadGatewayError`, `ServiceUnavailableError`), `Timeout`, `APIConnectionError`, and key-specific `AuthenticationError`/`PermissionDeniedError`. For example, a revoked key; Gemini returns 401 for an invalid key.
  - **Stop:** request-level errors (400, context window, unknown model). Another key would fail the same way, so the API returns `502 {"detail": "provider rejected the request"}`.
- `fallback_attempts` is the number of keys that failed before the successful one.
- If every key fails, the API returns **`503 {"detail": "all providers unavailable"}`**.
- **Why not `litellm.Router`?** Router cooldowns are process-wide state. A simulated or transient 429 on `key_1` would bench that key for *other* requests, which breaks the rule that simulation is per-request only. The Router also doesn't reliably report which deployment answered or how many attempts failed. LiteLLM still handles provider calls, error mapping, timeouts and caching; this app only adds the key ordering.
- **Secrets:** keys never appear in responses. A logging filter redacts any configured key from every log record, and `ApiKey.__repr__` hides the secret.

**Failure simulation** (`ENABLE_FAILURE_SIMULATION=true`): the `X-Simulate-Failure` header (`key_1=429`, `key_1=429,key_2=500`, `*=429`; statuses 429/500/502/503) is parsed into a per-request `FailurePlan`. Inside the same failover loop, a simulated key raises the exact LiteLLM exception the provider integration would raise (`RateLimitError`, `InternalServerError`, …). The normal failover handler catches it and moves on, and the next key makes a **genuine** LLM call. Nothing is stored between requests. Simulated requests skip the cache *read*, so `key_1=429` always demonstrates key_2 and `*=429` always returns 503, even for a prompt that is already cached. The fresh answer is still written to the cache. With simulation disabled, the header is ignored; a malformed header while enabled returns `400`.

### Caching

- LiteLLM's built-in cache: `litellm.cache = Cache(type="local")` (in-memory, `CACHE_TTL_SECONDS`, `CACHE_MAX_ITEMS`). LiteLLM 1.110 removed the disk cache type.
- LiteLLM builds the **cache key** from the completion arguments: the **model name** plus the **complete `messages` list** (system prompt, the whole previous conversation and the current user message). The API key is deliberately *not* in the key, so an answer produced by `key_2` can be reused.
  - The same first message in two new sessions produces an identical prompt, so it **hits** the cache.
  - A different message, system prompt, history or model produces a different key, so it **misses**.
- **Hit detection:** `response._hidden_params["cache_hit"] is True`, which LiteLLM's caching handler sets. There is no latency threshold.
- On a hit:
  - `resolution_source = LLM_CACHE`, `key_id = null`, `fallback_attempts = 0`
  - the reply text and stored `usage` (input/output tokens) are identical to the original response
  - latency is a few ms, versus hundreds for live inference
- Only the first attempt reads the cache, so a hit can never carry fallback attempts.
- **Write-before-reply:** LiteLLM writes the cache in a background task. After a live response the client awaits those pending writes, so an identical request sent right afterward reliably hits the cache. At startup the app also runs one mocked cache write and hit on a private sentinel prompt (no provider call). LiteLLM's lazy initialization happens then, so the first real hit is as fast as later ones (about 2 ms).

### SQLite session storage

- Schema, created automatically at startup:
  - `sessions(id, system_prompt, created_at, updated_at)`
  - `messages(id, session_id, role, content, metadata JSON, created_at)`, with an index on `(session_id, id)`
- Writes use stdlib `sqlite3` in WAL mode, run in a worker thread (`asyncio.to_thread`) with a short-lived connection per operation, so the event loop never blocks.
- **Failed requests are never stored.** The session upsert, the user message and the assistant message are written in **one `BEGIN IMMEDIATE` transaction, only after** the LLM succeeds. A 503 or 422 writes nothing; an unknown session still returns 404 afterward.
- The client sends only the new message. The backend loads the last `MAX_HISTORY_MESSAGES` messages from SQLite and builds `[system] + history + [user]`.
- A per-session `asyncio.Lock` serializes turns of the same conversation, so concurrent requests can't build on stale history.
- `system_prompt` is optional. It's stored on the session and reused on later turns; otherwise `DEFAULT_SYSTEM_PROMPT` applies. It is never returned by the history endpoint.
- The history endpoint returns messages oldest first. Assistant messages include their stored metadata: `resolution_source`, `input_tokens`, `output_tokens`, plus model, latency, key and fallbacks.

### Token and latency accounting

- `input_tokens` and `output_tokens` come from the provider's `usage` for **this call only**. `input_tokens` covers the full prompt including history; nothing is accumulated across turns.
- If a provider omits usage, the app falls back to `litellm.token_counter`. This is an estimate, since a local tokenizer can differ slightly from the provider's, and it is logged.
- `latency_ms` is measured on the server with `time.perf_counter()` around the whole resolution, including failed attempts.

---

## Verification: how to see failover, cache hits and per-message token usage

```bash
API=http://localhost:8000

# Health
curl -s $API/health
# {"status":"ok","keys_configured":2,"cache_enabled":true}

# Normal chat
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s-7f3a","message":"What is the capital of France?"}'
# metadata: resolution_source=LIVE_INFERENCE, key_id=key_1, fallback_attempts=0

# Multi-turn (the backend adds history from SQLite)
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s-7f3a","message":"What is its population?"}'
# input_tokens is larger than the first turn (full prompt incl. history), but it is per request

# Session history
curl -s $API/sessions/s-7f3a/messages
curl -s $API/sessions/unknown/messages        # 404 {"detail":"session not found"}

# Cache hit: same first message, brand-new session
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s-other","message":"What is the capital of France?"}'
# resolution_source=LLM_CACHE, identical reply and tokens, key_id=null, fallback_attempts=0, latency a few ms

# Key failover (requires ENABLE_FAILURE_SIMULATION=true)
curl -s -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"s-fail","message":"Name a prime number"}'
# key_id=key_2, fallback_attempts=1 (a genuine answer from key_2)
curl -s -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=500' \
  -d '{"session_id":"s-fail","message":"Another one"}'

# All providers failing
curl -s -i -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"s-dead","message":"hello"}'
# HTTP 503 {"detail":"all providers unavailable"}; then:
curl -s $API/sessions/s-dead/messages         # 404, nothing was stored

# Validation
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"s1"}'          # 422
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{bad json'                    # 422
```

**In the UI:**
- Every assistant reply shows chips for source (green `LIVE_INFERENCE` / purple `LLM_CACHE`), model, latency, input tokens, output tokens, key and fallbacks.
- To see a cache hit: open the app, send a message, click **New chat** (or reload), and send the same message.
- To see failover: click **Failover test**, enter `key_1=429`, and send. The reply shows `key_2` and `fallbacks 1`. With `*=429` the error appears as a chat message and nothing is saved.

**Persistence check:** send a few messages, run `docker compose restart backend` (or `make down && make up`), then `curl $API/sessions/<id>/messages`.

**What was verified while building this** (Docker stack plus an OpenAI-compatible stub provider reached over real HTTP via `LLM_API_BASE`):
- a real HTTP 429 on key_1 fell over to key_2
- the cache hit took 2 ms versus 744 ms live, with an identical reply and token counts
- multi-turn input tokens grew per request
- `*=429` returned 503 and stored nothing
- history survived `docker compose down` and `up`
- graceful SIGTERM shutdown
- the app process runs as non-root
- a headless-browser run of the UI covered all test IDs, Enter / Shift+Enter, the error bubble, a new session on reload, and mobile layout without horizontal scrolling

---

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_API_KEYS` | (required) | Comma-separated keys, at least 2. Order is key_1, key_2, … |
| `LLM_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model name |
| `LLM_MODELS` | empty | Optional per-key models (for example a different provider for key_2) |
| `LLM_API_BASE` | empty | Optional OpenAI-compatible endpoint |
| `LLM_TIMEOUT_SECONDS` | `30` | Per-attempt timeout |
| `LLM_MAX_OUTPUT_TOKENS` | empty | Optional `max_tokens` |
| `DEFAULT_SYSTEM_PROMPT` | `You are a helpful assistant.` | Used when the client sends none |
| `MAX_HISTORY_MESSAGES` | `50` | Context window, in stored messages |
| `DATABASE_URL` | `sqlite:////data/app.db` | SQLite file (must be on the persistent volume) |
| `CACHE_ENABLED` / `CACHE_TTL_SECONDS` / `CACHE_MAX_ITEMS` | `true` / `3600` / `1000` | LiteLLM cache |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | CORS origins, comma-separated |
| `ENABLE_FAILURE_SIMULATION` | `false` (compose: `true`) | Enables the `X-Simulate-Failure` header |
| `LOG_LEVEL` | `INFO` | |
| `API_BASE_URL` (frontend container) | empty | Backend URL, injected at container start into `/env-config.js` |
| `VITE_API_BASE_URL` (frontend build) | `http://localhost:8000` | Build-time backend URL for static hosts |

Compose maps `FRONTEND_API_BASE_URL` in `.env` to the frontend's `API_BASE_URL`.

---

## Deployment

The backend needs a host with **Docker and a persistent volume** mounted at `/data`. An ephemeral container filesystem would lose the SQLite database on every deploy.

### Railway (used for the live URLs)

```bash
npm i -g @railway/cli && railway login
railway init --name llm-chatbot
railway add --service backend && railway add --service frontend
railway service link backend && railway volume add -m /data         # persistent SQLite volume
railway domain --service backend --port 8000
railway domain --service frontend --port 3000

railway variable set --service backend --skip-deploys \
  DATABASE_URL=sqlite:////data/app.db LLM_MODEL=gemini/gemini-3.5-flash-lite \
  ENABLE_FAILURE_SIMULATION=true CACHE_ENABLED=true PORT=8000 \
  ALLOWED_ORIGINS=https://<frontend-domain>
echo -n "key1,key2" | railway variable set LLM_API_KEYS --stdin --service backend --skip-deploys
railway variable set --service frontend --skip-deploys API_BASE_URL=https://<backend-domain> PORT=3000

railway up backend  --path-as-root --service backend  --ci
railway up frontend --path-as-root --service frontend --ci
```

On Git Bash for Windows, run `export MSYS_NO_PATHCONV=1` first so `/data` isn't rewritten into a Windows path. Railway rejects a `VOLUME` instruction in a Dockerfile, so the backend image leaves `/data` to the mounted volume.

### Option A: Render (Blueprint, both tiers): `render.yaml`

1. Push this repo to GitHub. In Render, choose **New → Blueprint** and select the repo.
2. The backend runs on the `starter` plan (persistent disks need a paid instance), with a 1 GB disk at `/data`. The frontend runs on the free plan.
3. Set the secret env vars:
   - backend: `LLM_API_KEYS=key1,key2`, `ALLOWED_ORIGINS=https://<frontend>.onrender.com`
   - frontend: `API_BASE_URL=https://<backend>.onrender.com`
4. Deploy, then check: `curl https://<backend>.onrender.com/health`. Open the frontend and send a message.

### Option B: Fly.io backend (`backend/fly.toml`) and any static host for the frontend

```bash
cd backend
fly launch --no-deploy --copy-config --name <backend-app>
fly volumes create chat_data --size 1 --region iad
fly secrets set LLM_API_KEYS="key1,key2" ALLOWED_ORIGINS="https://<frontend-host>"
fly deploy
```

For the frontend, use either:
- the Docker image anywhere, with `API_BASE_URL=https://<backend-app>.fly.dev`, or
- Vercel/Netlify: root `frontend`, build `npm run build`, output `dist`, env `VITE_API_BASE_URL=https://<backend-app>.fly.dev`.

Run the backend as a **single instance**: SQLite and the in-memory LiteLLM cache are per-process.

---

## Setup Used: your editor and AI tools

- **Editor:** Visual Studio Code on Windows 11
- **AI tools:** Claude Code (Anthropic, model Claude Opus 5.5) in the VS Code extension, used for architecture, implementation, tests, Docker setup and docs
- **Verification:** pytest, curl, Docker Desktop (Compose v5), and headless Microsoft Edge via `playwright-core` for UI checks

---

## Bonus features (if any): what you added beyond the requirements and why

- **Failover test panel in the UI.** It sets the `X-Simulate-Failure` header from the browser, which makes the demo video and manual checks easy.
- **Key and fallback chips** on every reply, in addition to the required metadata, so failover is visible at a glance.
- **Per-key models (`LLM_MODELS`) and `LLM_API_BASE`.** key_2 can be a different provider, for real provider-level redundancy, or any OpenAI-compatible gateway.
- **Runtime-configurable frontend image.** `API_BASE_URL` is injected at container start, so one image works for every environment without a rebuild.
- **Hardening:**
  - API keys are redacted from all logs
  - the container runs as non-root (the entrypoint fixes volume ownership, then drops privileges)
  - health checks and graceful shutdown
  - per-session locking against concurrent turns
- **Real-provider integration test** (`pytest -m integration`), skipped automatically when no keys are set.

---

## Known limitations

- The LiteLLM cache is **in-memory** (this LiteLLM version has no disk cache type), so it resets when the backend restarts. Conversation history is persisted and unaffected.
- One backend process by design (SQLite and the in-process cache). Scaling out would need a shared cache such as Redis, which the assignment excludes.
- If a provider doesn't report usage, token counts are estimated locally.
- `litellm==1.110.2` imports `orjson` and `soundfile` on its cache-hit path without declaring them, so they are pinned explicitly in `requirements.txt`.
