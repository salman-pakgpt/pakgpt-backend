# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PakGPT is a minimal FastAPI chat backend backed by an LLM (via LiteLLM, so any provider LiteLLM supports can be swapped in through the per-task model settings), plus a Streamlit console for manually exercising it. It's a small flat prototype (`main.py`, `profile_manager.py`, `streamlit.py`), not a package — there's no build step, test suite, or linter configured.

## Running it

The app is run and tested **only through Docker Compose**, locally:
```
docker compose up --build
```
The API is at `http://127.0.0.1:8000`, the console at `http://127.0.0.1:8501` (both bound to localhost only, since `/chat` has no auth). Session history persists across `docker compose down && up` in the `sessions-data` named volume, mounted at `/app/data` in the `api` container (`DB_PATH` is set to `/app/data/sessions.db` there — mounting a volume at `/app` itself would shadow the image's code on every rebuild, so don't move it). There's no bind mount or `--reload`, so code changes need `docker compose up --build` to take effect.

Anything that touches the database (scripts, inspecting or seeding test rows) runs inside the container, e.g. `docker compose exec api python ...`. The image has Python but no `sqlite3` CLI.

`requirements-api.txt` and `requirements-streamlit.txt` list only each service's *direct* dependencies (pinned), letting pip resolve the transitive closure at build time. When a direct dependency changes, update the file for the service that uses it (`python-dotenv` is in both).

`docker/api.Dockerfile` copies only `main.py`, `profile_manager.py` and `scripts/` into the image, and `docker/streamlit.Dockerfile` copies only `streamlit.py`. Any new file a service needs at runtime must be added to its Dockerfile. Compose's `api` healthcheck calls `GET /`, and `streamlit` waits on it (`service_healthy`), so keep that route working.

The repo folder still holds a leftover `venv/` and `sessions.db` from before the Docker-only setup. Both are gitignored and dockerignored and kept on purpose, since `sessions.db` has older history that isn't in the volume. The app never reads that file, so don't query or seed it when verifying behavior.

## Configuration

Environment variables are loaded from `.env` (gitignored):
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` — provider keys consumed by LiteLLM based on which models are selected.
- Model strings are LiteLLM model names, e.g. `gemini/gemini-3.5-flash-lite`. Keep the `gemini/` prefix — a bare Gemini model name makes LiteLLM route to Vertex AI, which needs full GCP credentials rather than just `GEMINI_API_KEY`. Change the provider prefix to switch providers.
- `LLM_MODEL` — legacy fallback for `CHAT_LLM` only, default `gemini/gemini-3.5-flash-lite`. The local `.env` no longer sets it.
- The local `.env` also lists `LLM_MODEL_1`, `LLM_MODEL_2`, and so on. These are **a human reference list of available models only**. The app never reads them, so don't reference them from code or from other `.env` entries (no `${LLM_MODEL_2}`); settings use literal values.
- `CHAT_LLM`, `EXTRACTION_LLM`, `COMPACTION_LLM` — per-task models, each optional. Unset ones fall back in the order `COMPACTION_LLM` → `EXTRACTION_LLM` → `CHAT_LLM` → `LLM_MODEL`, so with none set everything uses `LLM_MODEL`. The local `.env` sets all three with literal values: `CHAT_LLM=gemini/gemini-3.8-flash`, and extraction and compaction on `gemini/gemini-3.5-flash-lite`, which has its own free-tier quota. LiteLLM rejects `reasoning_effort` for a `gemini/` model name it doesn't know, so use real model names.
- `PROFILE_*` — user-profile tuning, all optional. Unset, they keep the documented behavior:
  - `PROFILE_THRESHOLD_<CATEGORY>` and `PROFILE_TARGET_<CATEGORY>`: compaction thresholds and targets. The defaults are 100/60, and 200/120 for `ONGOING_CONTEXT`.
  - `PROFILE_MAX_INJECT_WORDS` (default 500): a word cap on the profile text injected into the prompt. An entry that would exceed it is left out whole, not cut mid-sentence, and a warning is logged. The markdown for `/profile` is never capped.
  - `PROFILE_EXTRACTION_MAX_TOKENS` and `PROFILE_COMPACTION_MAX_TOKENS` (default 500 each).
  - `PROFILE_EXTRACTION_ENABLED` and `PROFILE_COMPACTION_ENABLED`: kill switches, on by default. Set one to `false` or `0` to turn it off. The extraction check sits inside `process_message_for_profile`, so it applies to every caller.

  All of these are read in `main.py`, after `load_dotenv()`, and `profile_manager` reads them as `main.X` at call time.
- `LLM_REASONING_EFFORT` — optional override. Normally unset: `main.reasoning_effort(model)` picks `reasoning_effort` from each task's model's provider prefix via `REASONING_EFFORT_BY_PROVIDER` (`openai` → `minimal`, `gemini` → `low`, since `gemini-3.8-flash` rejects `minimal` with a 400), so switching a task's model setting alone is enough. Providers not in that table (e.g. `anthropic/`) send no reasoning parameter; add an entry after verifying what the provider accepts.
- `DB_PATH` — SQLite file path for session storage (default `sessions.db`).
- `API_URL` — used only by `streamlit.py` to reach the FastAPI backend (default `http://127.0.0.1:8000/chat`).

## Architecture

`main.py` is the entire backend:
- A single `POST /chat` endpoint takes `{session_id?, message, dry_run?}` and returns `{session_id, reply, context}`. `session_id` is generated (`uuid4`) when omitted, so a client's first call has no session and every later call in that conversation must pass back the returned `session_id`. `dry_run` (default `false`) skips the real LLM call entirely and returns a canned reply (`DRY_RUN_REPLY`) — zero cost, zero tokens — while still exercising the full request/DB write path.
- Conversation state lives in two SQLite tables (`sessions.db`): `messages` (one row per chat message: `session_id, role, content, created_at, is_test`) and `llm_calls` (one row per LLM call: tokens, `cost_usd`, `latency_ms`, `is_test`, linked to the assistant message it produced via `message_id`). Schema + indexes + `PRAGMA journal_mode=WAL` are created once at FastAPI startup via a `lifespan` handler (`init_db()`), not per request. `init_db()` also runs a one-line-per-column migration (`_ensure_column`, via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`) so `is_test` lands in a `sessions.db` created before that column existed — `CREATE TABLE IF NOT EXISTS` alone wouldn't add it to an existing table.
- `dry_run: true` requests are the only rows written with `is_test = 1` (model recorded as `"dry-run"`, tokens/cost zeroed) — this is separate from the `test-<purpose>-<timestamp>` session-naming convention below, which marks real (paid) test calls that still get `is_test = 0`. `scripts/llm_usage.py`'s three reports filter out `is_test = 1` rows so dry runs never show up in cost/usage numbers.
- `get_recent_messages()` queries only the last `MAX_HISTORY_MESSAGES` (20) rows for a session (`ORDER BY id DESC LIMIT n`, reversed in Python) — the full history is never loaded. `save_turn()` writes the user message, assistant message, and the `llm_calls` row in one transaction, only after a successful LLM call; a failed call writes nothing.
- The model call goes through `litellm.completion`, which normalizes providers behind one interface — swapping a model's prefix (`openai/`, `anthropic/`, `gemini/`, etc.) is enough to change providers without touching the call site. A fixed system prompt instructs the model to reply in 1-3 sentences; `max_tokens=500`, a 30s timeout, and 1 retry are hardcoded (`num_retries` depends on `tenacity`, a direct dependency for that reason — without it, a failed first attempt returns a misleading "tenacity import failed" 502 instead of retrying). Cost for Gemini models is computed from LiteLLM's paid-tier prices, so `cost_usd` is non-zero even on the free tier (a paid-equivalent figure, not the actual bill).
- Cost is computed via `litellm.completion_cost(completion_response=response)`; if the model isn't in litellm's cost map this raises, so it's wrapped in a try/except that stores `cost_usd = NULL` and logs a warning naming the model — the request still succeeds. `latency_ms` wraps only the `completion()` call.
- Token usage and cost-calculation warnings go through `logging` (`logging.basicConfig(level=logging.INFO)`), not `print()`.
- Errors from the LLM call are caught broadly and surfaced as a 502 with the underlying exception message.
- `scripts/llm_usage.py` is an on-demand reporting script: it queries `llm_calls` and writes (overwriting) three CSVs into a `reports/` folder (gitignored) — `llm_usage_by_session.csv`, `llm_usage_by_model.csv`, `llm_usage_by_day.csv` — each with call count, token totals, `cost_usd`, and `avg_latency_ms` for that grouping. Run it with `docker compose exec api python scripts/llm_usage.py`. That writes the CSVs inside the container, so copy them out with `docker compose cp api:/app/reports/. ./reports/docker`.

`streamlit.py` is a standalone manual test console, not part of the API: it POSTs to `API_URL`, keeps a client-side `st.session_state.history` of all turns in the run, and lets you inspect the exact `context` (message list) sent to the model for each turn via an expander. A right-hand pane renders `GET /profile`, whose address is derived from `API_URL`. It re-fetches on every rerun and has a "Refresh profile" button. It is not authoritative for session state — the backend's SQLite store is. It never sends `dry_run`, so **every message sent from the console is a real, paid LLM call**. Its `requests.post` timeout (30s) equals the API's LLM timeout, so a call that needs its one retry can time out in the console even though the API finishes and saves the turn.

**User profile (`profile_manager.py`; see `PRODUCT_SPEC.md` §9 and `docs/profile_module_requirements.md`):**
- A per-user `profile` table holds durable facts in four categories: `identity`, `ongoing_context`, `preference` and `instruction`. Each entry's `status` is `active`, `superseded` or `deleted`. Entries are never deleted as rows; they are only re-marked.
- `messages` and `llm_calls` both have a `user_id` column. Everything uses `DEFAULT_USER_ID = "local_user"` until Phase 2 adds logins.
- `chat()` appends the active profile to the single system message under `PROFILE_HEADER`, which tells the model that the profile wins over `SYSTEM_PROMPT`. This also happens on dry runs, so seed `profile` rows and make a dry run to check injection for free.
- `GET /profile` returns `{"markdown": ...}` for the console's right-hand pane. It deliberately takes no `user_id` parameter.
- `save_turn` takes `user_id` and returns the user message's id, for extraction.
- `profile_manager` does `import main` and reads config as `main.X` at call time. The circular import is deliberate: `main` imports `profile_manager` at the top, so never use `from main import ...` there.
- **Extraction:** after each non-dry-run reply, `chat()` schedules `process_message_for_profile` as a `BackgroundTasks` job. It makes one structured-output call (`response_format` set to a Pydantic model), validates each `add`/`update`/`delete` against the user's active entries, and applies the valid ones in one transaction. Jobs run one at a time under a module lock, and failures are logged, never raised.
- **Compaction:** runs for a category the message touched once that category passes its `main.PROFILE_WORD_LIMITS` threshold. It supersedes the old entries and inserts the rewrite in one transaction, and a failed call or an empty result changes nothing. Compaction entries record the triggering message as their source.
- **Logging:** both calls are logged to `llm_calls` with `call_type` set to `extraction` or `compaction`, and `message_id` set to the user message. Token and cost handling is shared with `chat()` through `main.response_usage()`.
- **Free testing:** to test extraction and compaction without paying, patch `profile_manager.completion` (and `main.completion`) with a wrapper around `litellm.completion(..., mock_response=<json>)`, and run it against a copy of the database: `docker compose run --rm --no-deps -T -e DB_PATH=/tmp/x.db api python -`. From Git Bash, prefix the command with `MSYS_NO_PATHCONV=1`, or the `/tmp` path gets rewritten.
- **Real calls are scarce:** Gemini's free tier allows only 20 requests per day per model, and each real chat message now uses 2 or more. Retries and failed attempts count too.

`scripts/llm_usage.py` imports `DB_PATH` from `main`. Renaming it breaks the script, and importing `main` also loads `.env` and `litellm`. A comment in `main.py` asks that the app's SQL stay in its helper functions so that a later Postgres switch touches only that file.

`PRODUCT_SPEC.md` §9–10 list what comes next, in order. §9 is the user profile module (`profile_manager.py`, extraction through `BackgroundTasks`, a live profile pane), with sequenced action items and review gaps. §10 is Phase 2: user accounts, a per-user daily cap, and Caddy/HTTPS deployment. Read the relevant section before starting work in either area.

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
- **Keep `PRODUCT_SPEC.md` current too.** It's the handover doc (current state + next steps). When a change ships, update its affected sections, tick or rewrite the relevant next steps, add a decision-log row for any real decision, and bump its "Last updated" date and commit.

## Testing & manual verification

Every LLM call through `/chat` is real — it spends real tokens and real money, and (post-refactor) it's permanently logged in `llm_calls` alongside genuine usage. Keep that in mind when verifying behavior:

- **`openai/gpt-5-nano` is reserved for the owner's manual testing.** AI agents must never make a real call to it. Test with no LLM at all (dry runs, `mock_response` mocks), or, when real model output is needed, with a `gemini/` model. The owner's `.env` may point any task at gpt-5-nano, so an agent's real call must override the model explicitly in a one-off container, e.g. `docker compose run --rm --no-deps -T -e CHAT_LLM=gemini/... -e EXTRACTION_LLM=gemini/... -e COMPACTION_LLM=gemini/... api python -`. Never send real calls through the live stack while `.env` points at gpt-5-nano.
- **Use `dry_run: true` for anything that just needs to exercise the request/DB path** (a new field on `ChatRequest`, a schema change, the read/trim logic) — it costs nothing and skips the real model call. Reach for a real call only when the thing under test is actual model behavior (prompt wording, response quality, provider-specific quirks).
- **Test session_ids must be clearly marked and timestamped**, e.g. `test-<purpose>-<UTC YYYYMMDDHHMMSS>` (e.g. `test-ctxwindow-20260927154812`) — never a bare, reusable name like `ctx-test`. This keeps repeated test runs from landing on the same `session_id` (so they don't visually merge into what looks like one long conversation) and keeps them identifiable later in `llm_usage_*.csv`/`llm_calls` as test traffic, not real usage.
- Dry-run smoke test (with the API running): `curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" -d '{"session_id": "test-dryrun-<UTC YYYYMMDDHHMMSS>", "message": "hello", "dry_run": true}'`
- **Minimize the number of real LLM calls a test needs.** Every live `/chat` call costs tokens; don't spend 25 of them to prove something that doesn't require 25 real completions. E.g. to verify context-window trimming, seed the `messages` table directly via SQL (fake rows, no LLM involved) to build up history, then make exactly one real `/chat` call to confirm the read/trim path — not a loop of real calls building history one at a time.
- **Test data is never auto-deleted** — it accumulates in `sessions.db`/`llm_calls` exactly like real usage, and stays there. Clear naming (above) is how it's kept distinguishable, not cleanup after the fact. Deleting rows is a real, destructive action — only do it if explicitly asked.

## Known gaps to be aware of

- No automated tests exist in this repo.
- `*.db` is gitignored, so `sessions.db` stays untracked — it's never meant to be committed (it contains real conversation history).
