import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from litellm import completion, completion_cost

load_dotenv()
logging.basicConfig(level=logging.INFO)

# One row per chat message (messages) plus one row per LLM call (llm_calls,
# linked via message_id to the assistant reply it produced). Keep all SQL in
# these helper functions - a later Postgres switch touches only this file.
DB_PATH = os.getenv("DB_PATH", "sessions.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_id INTEGER,
    call_type TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER,
    cost_usd REAL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_session_id ON llm_calls(session_id);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    # covers DBs created before is_test existed - CREATE TABLE IF NOT EXISTS above is a no-op on them
    _ensure_column(conn, "messages", "is_test", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "llm_calls", "is_test", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def get_recent_messages(session_id: str, limit: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def save_turn(
    session_id: str,
    user_message: str,
    assistant_reply: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int | None,
    cost_usd: float | None,
    latency_ms: int,
    is_test: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (session_id, role, content, created_at, is_test) VALUES (?, ?, ?, ?, ?)",
            (session_id, "user", user_message, now, is_test),
        )
        cur.execute(
            "INSERT INTO messages (session_id, role, content, created_at, is_test) VALUES (?, ?, ?, ?, ?)",
            (session_id, "assistant", assistant_reply, now, is_test),
        )
        message_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO llm_calls (
                session_id, message_id, call_type, model,
                input_tokens, output_tokens, reasoning_tokens,
                cost_usd, latency_ms, created_at, is_test
            ) VALUES (?, ?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, message_id, model, input_tokens, output_tokens,
             reasoning_tokens, cost_usd, latency_ms, now, is_test),
        )
        conn.commit()
    finally:
        conn.close()


MODEL = os.getenv("LLM_MODEL", "openai/gpt-5-nano")
MAX_HISTORY_MESSAGES = 20  # simple context guard - keep only the recent tail
MAX_OUTPUT_TOKENS = 500  # headroom for reasoning + a short reply - avoids empty responses
LLM_TIMEOUT_SECONDS = 30  # fail instead of hanging forever if the provider stalls
LLM_MAX_RETRIES = 1  # one retry for transient errors (timeouts, rate limits, etc.)

SYSTEM_PROMPT = {
    "role": "system",
    "content": "Respond in 1-3 sentences only. Be concise and direct.",
}

DRY_RUN_REPLY = "This is a canned dry-run reply - no LLM call was made."


class ChatRequest(BaseModel):
    session_id: str | None = None  # omit on the first message, we'll create one
    message: str
    dry_run: bool = False  # skip the real LLM call - zero cost, for testing the request/DB path


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    context: list[dict]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    history = get_recent_messages(session_id, MAX_HISTORY_MESSAGES)

    history.append({"role": "user", "content": req.message})

    # Only send the recent tail to the model - keeps token usage and
    # context length bounded as a conversation grows.
    trimmed = history[-MAX_HISTORY_MESSAGES:]
    messages = [SYSTEM_PROMPT] + trimmed

    if req.dry_run:
        reply = DRY_RUN_REPLY
        save_turn(
            session_id=session_id,
            user_message=req.message,
            assistant_reply=reply,
            model="dry-run",
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=None,
            cost_usd=0.0,
            latency_ms=0,
            is_test=True,
        )
        return ChatResponse(session_id=session_id, reply=reply, context=messages)

    start = time.monotonic()
    try:
        response = completion(
            model=MODEL,
            messages=messages,
            reasoning_effort="minimal",
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout=LLM_TIMEOUT_SECONDS,
            num_retries=LLM_MAX_RETRIES,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e}")
    latency_ms = int((time.monotonic() - start) * 1000)

    reply = response.choices[0].message.content or "(no response generated)"

    usage = response.usage
    reasoning_tokens = None
    if usage.completion_tokens_details:
        reasoning_tokens = usage.completion_tokens_details.reasoning_tokens
    output_tokens = usage.completion_tokens - (reasoning_tokens or 0)

    try:
        cost_usd = completion_cost(completion_response=response)
    except Exception:
        cost_usd = None
        logging.warning(f"completion_cost failed for model={MODEL}")

    logging.info(
        f"[tokens] input={usage.prompt_tokens} "
        f"thinking={reasoning_tokens or 0} "
        f"output={output_tokens} "
        f"total={usage.total_tokens}"
    )

    save_turn(
        session_id=session_id,
        user_message=req.message,
        assistant_reply=reply,
        model=MODEL,
        input_tokens=usage.prompt_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )

    return ChatResponse(session_id=session_id, reply=reply, context=messages)


@app.get("/")
def root():
    return {"status": "ok"}
