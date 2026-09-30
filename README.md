# PakGPT

A minimal chat backend: one FastAPI endpoint that sends a conversation to an LLM and stores the history in SQLite. A Streamlit console is included for trying it out by hand.

Models are called through [LiteLLM](https://docs.litellm.ai/), so you switch providers (Gemini, OpenAI, Anthropic, …) by changing one environment variable. The default is Google's `gemini-3.5-flash-lite`, which is on the free tier.

## Quick start

1. Create a `.env` file in the repo root (see [Configuration](#configuration)).
2. Start both services:
   ```
   docker compose up --build
   ```
3. Open the test console at http://127.0.0.1:8501. The API itself is at http://127.0.0.1:8000.

Both ports are bound to `127.0.0.1` only, because `/chat` has no authentication. Chat history is kept in the `sessions-data` Docker volume, so it survives `docker compose down` and `up`.

## Configuration

Settings are read from `.env`, which is gitignored and never committed:

```
GEMINI_API_KEY=your-key
OPENAI_API_KEY=your-key            # only needed for openai/ models

LLM_MODEL=gemini/gemini-3.8-flash
```

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model name. The prefix picks the provider. |
| `CHAT_LLM` | `LLM_MODEL` | Model for chat replies. |
| `EXTRACTION_LLM` | `CHAT_LLM` | Model that pulls profile facts out of each message. |
| `COMPACTION_LLM` | `EXTRACTION_LLM` | Model that shortens a profile category once it grows past its limit. |
| `PROFILE_THRESHOLD_<CATEGORY>` / `PROFILE_TARGET_<CATEGORY>` | 100 / 60 (`ONGOING_CONTEXT`: 200 / 120) | Word count that triggers compaction, and the size to compact to. `<CATEGORY>` is `IDENTITY`, `ONGOING_CONTEXT`, `PREFERENCE` or `INSTRUCTION`. |
| `PROFILE_MAX_INJECT_WORDS` | 500 | Word cap on the profile added to the prompt. |
| `PROFILE_EXTRACTION_MAX_TOKENS` / `PROFILE_COMPACTION_MAX_TOKENS` | 500 / 500 | Output token caps for the two profile calls. |
| `PROFILE_EXTRACTION_ENABLED` / `PROFILE_COMPACTION_ENABLED` | `true` / `true` | Kill switches. `false` turns the step off. |
| `LLM_REASONING_EFFORT` | chosen per provider | Optional override. Normally leave unset (see below). |
| `DB_PATH` | `sessions.db` | SQLite file for chat history. |
| `API_URL` | `http://127.0.0.1:8000/chat` | Where the Streamlit console sends requests. |

- **Keep the provider prefix on Gemini models** (`gemini/…`). Without it, LiteLLM routes to Vertex AI, which needs full Google Cloud credentials instead of an API key.
- **Reasoning effort is picked automatically**: `minimal` for `openai/` models and `low` for `gemini/` models (`gemini-3.8-flash` rejects `minimal`). Changing `LLM_MODEL` is enough to switch providers.
- **Free-tier quota is per model** (20 requests a day on `gemini-3.8-flash`). A real chat message uses 2 or more requests because of profile extraction. Putting `EXTRACTION_LLM`/`COMPACTION_LLM` on a different model, such as `gemini/gemini-3.5-flash-lite`, gives those steps their own quota.
- To keep several models on hand, list them as `LLM_MODEL_1=…`, `LLM_MODEL_2=…` and select one with `LLM_MODEL=${LLM_MODEL_2}`. Both local runs and Docker Compose expand the reference.

## API

`POST /chat`

```json
{ "session_id": "optional", "message": "What is the capital of Pakistan?", "dry_run": false }
```

Returns `{ "session_id", "reply", "context" }`, where `context` is the exact message list sent to the model.

- Leave out `session_id` on the first message; a new one is created and returned. Send it back on every later message to continue that conversation.
- Only the most recent 20 messages of a session are sent to the model.
- LLM failures return HTTP 502 with the provider's error message. Calls time out after 30 seconds and are retried once.

`GET /profile` returns the stored user profile as markdown: `{ "markdown": "..." }`. The profile holds durable facts about the user, and it is appended to the system prompt on every chat call. After each real (non-dry-run) reply, a background step makes a second LLM call to pull new facts from the user's message. A new fact therefore appears in the profile shortly after the reply, not with it. Dry runs skip this step. The test console shows it in a pane on the right.

`GET /` is a health check that returns `{"status": "ok"}`.

## Testing without spending money

Every normal `/chat` call is a real model call, and it is logged as usage. To test the request and database path for free, set `"dry_run": true`:

```
curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" \
  -d '{"session_id": "test-dryrun-20260928120000", "message": "hello", "dry_run": true}'
```

A dry run returns a canned reply and stores rows marked `is_test = 1` with zero tokens and cost. These rows are left out of the usage reports.

When you do need a real test call, use a session id like `test-<purpose>-<UTC timestamp>` so test traffic is easy to tell apart from real usage later. Test rows are never deleted automatically.

## Usage reports

`docker compose exec api python scripts/llm_usage.py` writes three CSV reports to `reports/` inside the container: usage by session, by model, and by day (calls, tokens, cost, average latency). Copy them out with `docker compose cp api:/app/reports/. ./reports/docker`.

Note on cost: LiteLLM prices Gemini calls at paid-tier rates, so `cost_usd` shows small amounts even on the free tier. Treat it as what the traffic would cost on a paid plan, not as your actual bill.

## Project layout

```
main.py                     the API: endpoints, model call, SQLite storage
profile_manager.py          user profile: read, format, extraction, compaction
streamlit.py                manual test console with a live profile pane (not part of the API)
docs/                       feature requirements
scripts/                    usage report script
docker/, docker-compose.yml container setup for both services
requirements-*.txt          pinned dependencies for each container
```

There is no automated test suite yet.
