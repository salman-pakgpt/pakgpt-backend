# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PakGPT is a minimal FastAPI chat backend backed by an LLM (via LiteLLM, so any provider LiteLLM supports can be swapped in through `LLM_MODEL`), plus a Streamlit console for manually exercising it. It's a two-file prototype, not a package — there's no build step, test suite, or linter configured.

## Running it

Activate the venv first (Windows): `venv\Scripts\activate.bat`

Start the API:
```
uvicorn main:app --reload --port 8000
```

Start the test console (in a separate terminal, API must already be running):
```
streamlit run streamlit.py
```

Install/update dependencies: `pip install -r requirements.txt`

### Docker

`docker compose up --build` runs both services. The API is at `http://127.0.0.1:8000`, the console at `http://127.0.0.1:8501` (both bound to localhost only, since `/chat` has no auth). Session history persists across `docker compose down && up` in the `sessions-data` named volume, mounted at `/app/data` in the `api` container (`DB_PATH` is set to `/app/data/sessions.db` there — mounting a volume at `/app` itself would shadow the image's code on every rebuild, so don't move it).

`requirements-api.txt` and `requirements-streamlit.txt` list only each service's *direct* dependencies (pinned), letting pip resolve the transitive closure at build time — `requirements.txt` (the full frozen set for local/venv use) isn't safe to split by hand, since packages like `tiktoken`/`huggingface-hub` look Streamlit-related but are actually pulled in by `litellm`. Keep all three files in sync when a direct dependency's version changes.

## Configuration

Environment variables are loaded from `.env` (gitignored):
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` — provider keys consumed by LiteLLM based on which `LLM_MODEL` is selected.
- `LLM_MODEL` — LiteLLM model string, e.g. `gemini/gemini-3.5-flash-lite` (default, free tier). Keep the `gemini/` prefix — a bare Gemini model name makes LiteLLM route to Vertex AI, which needs full GCP credentials rather than just `GEMINI_API_KEY`. Change the provider prefix to switch providers. The local `.env` keeps candidate models as numbered `LLM_MODEL_<n>` entries and selects one with e.g. `LLM_MODEL=${LLM_MODEL_2}`; the app reads only `LLM_MODEL`, and both python-dotenv and Docker Compose's `env_file` expand the `${...}` reference.
- `LLM_REASONING_EFFORT` — optional override. Normally unset: `main.py` picks `reasoning_effort` from `LLM_MODEL`'s provider prefix via `REASONING_EFFORT_BY_PROVIDER` (`openai` → `minimal`, `gemini` → `low`, since `gemini-3.8-flash` rejects `minimal` with a 400), so switching `LLM_MODEL` alone is enough. Providers not in that table (e.g. `anthropic/`) send no reasoning parameter; add an entry after verifying what the provider accepts.
- `DB_PATH` — SQLite file path for session storage (default `sessions.db`).
- `API_URL` — used only by `streamlit.py` to reach the FastAPI backend (default `http://127.0.0.1:8000/chat`).

## Architecture

`main.py` is the entire backend:
- A single `POST /chat` endpoint takes `{session_id?, message, dry_run?}` and returns `{session_id, reply, context}`. `session_id` is generated (`uuid4`) when omitted, so a client's first call has no session and every later call in that conversation must pass back the returned `session_id`. `dry_run` (default `false`) skips the real LLM call entirely and returns a canned reply (`DRY_RUN_REPLY`) — zero cost, zero tokens — while still exercising the full request/DB write path.
- Conversation state lives in two SQLite tables (`sessions.db`): `messages` (one row per chat message: `session_id, role, content, created_at, is_test`) and `llm_calls` (one row per LLM call: tokens, `cost_usd`, `latency_ms`, `is_test`, linked to the assistant message it produced via `message_id`). Schema + indexes + `PRAGMA journal_mode=WAL` are created once at FastAPI startup via a `lifespan` handler (`init_db()`), not per request. `init_db()` also runs a one-line-per-column migration (`_ensure_column`, via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`) so `is_test` lands in a `sessions.db` created before that column existed — `CREATE TABLE IF NOT EXISTS` alone wouldn't add it to an existing table.
- `dry_run: true` requests are the only rows written with `is_test = 1` (model recorded as `"dry-run"`, tokens/cost zeroed) — this is separate from the `test-<purpose>-<timestamp>` session-naming convention below, which marks real (paid) test calls that still get `is_test = 0`. `scripts/llm_usage.py`'s three reports filter out `is_test = 1` rows so dry runs never show up in cost/usage numbers.
- `get_recent_messages()` queries only the last `MAX_HISTORY_MESSAGES` (20) rows for a session (`ORDER BY id DESC LIMIT n`, reversed in Python) — the full history is never loaded. `save_turn()` writes the user message, assistant message, and the `llm_calls` row in one transaction, only after a successful LLM call; a failed call writes nothing.
- The model call goes through `litellm.completion`, which normalizes providers behind one interface — swapping `LLM_MODEL`'s prefix (`openai/`, `anthropic/`, `gemini/`, etc.) is enough to change providers without touching the call site. A fixed system prompt instructs the model to reply in 1-3 sentences; `max_tokens=500`, a 30s timeout, and 1 retry are hardcoded (`num_retries` depends on `tenacity`, a direct dependency for that reason — without it, a failed first attempt returns a misleading "tenacity import failed" 502 instead of retrying). Cost for Gemini models is computed from LiteLLM's paid-tier prices, so `cost_usd` is non-zero even on the free tier (a paid-equivalent figure, not the actual bill).
- Cost is computed via `litellm.completion_cost(completion_response=response)`; if the model isn't in litellm's cost map this raises, so it's wrapped in a try/except that stores `cost_usd = NULL` and logs a warning naming the model — the request still succeeds. `latency_ms` wraps only the `completion()` call.
- Token usage and cost-calculation warnings go through `logging` (`logging.basicConfig(level=logging.INFO)`), not `print()`.
- Errors from the LLM call are caught broadly and surfaced as a 502 with the underlying exception message.
- `scripts/migrate_sessions.py` is a one-off migration for the previous JSON-blob schema: it copies each legacy session's messages into `messages` (in original order; every migrated message gets that session's old `updated_at` as its `created_at`, since the blob format never recorded per-message timestamps) and renames the old `sessions` table to `sessions_legacy` (never dropped). Safe to re-run — it no-ops once `sessions` no longer exists. Run locally with `python scripts/migrate_sessions.py` (from the repo root, matching `DB_PATH`'s default), or against the Docker volume with `docker compose exec api python scripts/migrate_sessions.py`.
- `scripts/llm_usage.py` is an on-demand reporting script: it queries `llm_calls` and writes (overwriting) three CSVs into a `reports/` folder (gitignored) — `llm_usage_by_session.csv`, `llm_usage_by_model.csv`, `llm_usage_by_day.csv` — each with call count, token totals, `cost_usd`, and `avg_latency_ms` for that grouping. Run the same way as the migration script: locally (`python scripts/llm_usage.py`) or against the Docker volume (`docker compose exec api python scripts/llm_usage.py`).

`streamlit.py` is a standalone manual test console, not part of the API: it POSTs to `API_URL`, keeps a client-side `st.session_state.history` of all turns in the run, and lets you inspect the exact `context` (message list) sent to the model for each turn via an expander. It is not authoritative for session state — the backend's SQLite store is.

## Development principles

This is a lean prototype. Keep it that way.

- **Smallest change that works.** Solve exactly what was asked. No speculative features, config options, or "for later" abstractions.
- **Extend, don't add.** Prefer editing existing functions over new files, classes, or layers. Reuse `init_db`/`get_recent_messages`/`save_turn` rather than writing parallel logic. Keep the two-file structure unless asked to split it.
- **No new dependencies** without asking first.
- **No boilerplate.** No single-use wrapper functions or classes, no comments that restate the code, no defensive checks for states that can't happen.
- **Stay in scope.** Don't refactor, rename, or reformat code unrelated to the task.
- **Efficient at runtime.** No repeated per-request work that can happen once at startup, no redundant DB or network calls.
- **Plan first, with a size estimate.** Before implementing, state the files touched and approximate lines changed. If a change will exceed ~50 lines, stop and explain why before writing it.
- **After implementing**, summarize the diff in 2–3 lines and flag anything that could be removed.
- **Keep this file current.** Update CLAUDE.md in the same change when behavior, config, or run instructions change.

## Testing & manual verification

Every LLM call through `/chat` is real — it spends real tokens and real money, and (post-refactor) it's permanently logged in `llm_calls` alongside genuine usage. Keep that in mind when verifying behavior:

- **Use `dry_run: true` for anything that just needs to exercise the request/DB path** (a new field on `ChatRequest`, a schema change, the read/trim logic) — it costs nothing and skips the real model call. Reach for a real call only when the thing under test is actual model behavior (prompt wording, response quality, provider-specific quirks).
- **Test session_ids must be clearly marked and timestamped**, e.g. `test-<purpose>-<UTC YYYYMMDDHHMMSS>` (e.g. `test-ctxwindow-20260927154812`) — never a bare, reusable name like `ctx-test`. This keeps repeated test runs from landing on the same `session_id` (so they don't visually merge into what looks like one long conversation) and keeps them identifiable later in `llm_usage_*.csv`/`llm_calls` as test traffic, not real usage.
- **Minimize the number of real LLM calls a test needs.** Every live `/chat` call costs tokens; don't spend 25 of them to prove something that doesn't require 25 real completions. E.g. to verify context-window trimming, seed the `messages` table directly via SQL (fake rows, no LLM involved) to build up history, then make exactly one real `/chat` call to confirm the read/trim path — not a loop of real calls building history one at a time.
- **Test data is never auto-deleted** — it accumulates in `sessions.db`/`llm_calls` exactly like real usage, and stays there. Clear naming (above) is how it's kept distinguishable, not cleanup after the fact. Deleting rows is a real, destructive action — only do it if explicitly asked.

## Known gaps to be aware of

- No automated tests exist in this repo.
- `*.db` is gitignored, so `sessions.db` stays untracked — it's never meant to be committed (it contains real conversation history).
