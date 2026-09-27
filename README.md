# PakGPT

A minimal chat backend: one FastAPI endpoint that sends a conversation to an LLM and stores the history in SQLite. A Streamlit console is included for trying it out by hand.

Models are called through [LiteLLM](https://docs.litellm.ai/), so you switch providers (Gemini, OpenAI, Anthropic, …) by changing one environment variable. The default is Google's `gemini-3.5-flash-lite`, which is on the free tier.

## Quick start (Docker)

1. Create a `.env` file in the repo root (see [Configuration](#configuration)).
2. Start both services:
   ```
   docker compose up --build
   ```
3. Open the test console at http://127.0.0.1:8501. The API itself is at http://127.0.0.1:8000.

Both ports are bound to `127.0.0.1` only, because `/chat` has no authentication. Chat history is kept in the `sessions-data` Docker volume, so it survives `docker compose down` and `up`.

## Running without Docker

Requires Python 3.11.

```
python -m venv venv
venv\Scripts\activate.bat          # Windows; use `source venv/bin/activate` elsewhere
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

In a second terminal (with the API running): `streamlit run streamlit.py`

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
| `LLM_REASONING_EFFORT` | chosen per provider | Optional override. Normally leave unset (see below). |
| `DB_PATH` | `sessions.db` | SQLite file for chat history. |
| `API_URL` | `http://127.0.0.1:8000/chat` | Where the Streamlit console sends requests. |

- **Keep the provider prefix on Gemini models** (`gemini/…`). Without it, LiteLLM routes to Vertex AI, which needs full Google Cloud credentials instead of an API key.
- **Reasoning effort is picked automatically**: `minimal` for `openai/` models and `low` for `gemini/` models (`gemini-3.8-flash` rejects `minimal`). Changing `LLM_MODEL` is enough to switch providers.
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

`GET /` is a health check that returns `{"status": "ok"}`.

## Testing without spending money

Every normal `/chat` call is a real model call, and it is logged as usage. To test the request and database path for free, set `"dry_run": true`:

```
curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" \
  -d '{"session_id": "test-dryrun-20260928120000", "message": "hello", "dry_run": true}'
```

A dry run returns a canned reply and stores rows marked `is_test = 1` with zero tokens and cost. These rows are left out of the usage reports.

When you do need a real test call, use a session id like `test-<purpose>-<UTC timestamp>` so test traffic is easy to tell apart from real usage later. Test rows are never deleted automatically.

## Scripts

- `python scripts/llm_usage.py` writes three CSV reports to `reports/`: usage by session, by model, and by day (calls, tokens, cost, average latency).
- `python scripts/migrate_sessions.py` is a one-time migration from the old single-table session format. It is safe to re-run.

Against the Docker database, run them inside the container, e.g. `docker compose exec api python scripts/llm_usage.py`.

Note on cost: LiteLLM prices Gemini calls at paid-tier rates, so `cost_usd` shows small amounts even on the free tier. Treat it as what the traffic would cost on a paid plan, not as your actual bill.

## Project layout

```
main.py                     the whole API: endpoint, model call, SQLite storage
streamlit.py                manual test console (not part of the API)
scripts/                    usage reports and the one-off migration
docker/, docker-compose.yml container setup for both services
requirements*.txt           full local set, plus per-container lists for Docker
```

There is no automated test suite yet.
