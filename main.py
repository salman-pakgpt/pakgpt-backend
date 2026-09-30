import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from litellm import completion, completion_cost

import profile_manager

load_dotenv()
logging.basicConfig(level=logging.INFO)

# One row per chat message (messages) plus one row per LLM call (llm_calls,
# linked via message_id to the assistant reply it produced), plus the user
# profile (profile). Keep all SQL in helper functions here and in
# profile_manager.py - a later Postgres switch touches only those two files.
DB_PATH = os.getenv("DB_PATH", "sessions.db")
DEFAULT_USER_ID = "local_user"  # single hardcoded user until Phase 2 adds logins

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0,
    user_id TEXT NOT NULL DEFAULT 'local_user'
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
    is_test INTEGER NOT NULL DEFAULT 0,
    user_id TEXT NOT NULL DEFAULT 'local_user'
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_session_id ON llm_calls(session_id);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'local_user',
    category TEXT NOT NULL CHECK (category IN ('identity','ongoing_context','preference','instruction')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','deleted')),
    source_session_id TEXT,
    source_message_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'extraction' CHECK (source_type IN ('extraction','compaction')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_active ON profile(user_id, category, status);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    # covers DBs created before these columns existed - CREATE TABLE IF NOT EXISTS above is a no-op on them
    _ensure_column(conn, "messages", "is_test", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "llm_calls", "is_test", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "messages", "user_id", "TEXT NOT NULL DEFAULT 'local_user'")
    _ensure_column(conn, "llm_calls", "user_id", "TEXT NOT NULL DEFAULT 'local_user'")
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
    user_id: str,
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
) -> int:
    """Returns the user message's id, which profile extraction links back to."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (user_id, session_id, role, content, created_at, is_test) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, session_id, "user", user_message, now, is_test),
        )
        user_message_id = cur.lastrowid
        cur.execute(
            "INSERT INTO messages (user_id, session_id, role, content, created_at, is_test) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, session_id, "assistant", assistant_reply, now, is_test),
        )
        message_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO llm_calls (
                user_id, session_id, message_id, call_type, model,
                input_tokens, output_tokens, reasoning_tokens,
                cost_usd, latency_ms, created_at, is_test
            ) VALUES (?, ?, ?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, session_id, message_id, model, input_tokens, output_tokens,
             reasoning_tokens, cost_usd, latency_ms, now, is_test),
        )
        conn.commit()
    finally:
        conn.close()
    return user_message_id


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def _env_flag(name: str) -> bool:
    """On unless set to a false-ish value (0/false/no/off)."""
    return (os.getenv(name) or "true").strip().lower() not in ("0", "false", "no", "off")


# One model per task, each falling back to the previous so unset vars change nothing:
# COMPACTION_LLM -> EXTRACTION_LLM -> CHAT_LLM -> LLM_MODEL. Free-tier quota is per model,
# so splitting tasks across models also splits the quota.
CHAT_LLM = os.getenv("CHAT_LLM") or os.getenv("LLM_MODEL") or "gemini/gemini-3.5-flash-lite"
EXTRACTION_LLM = os.getenv("EXTRACTION_LLM") or CHAT_LLM
COMPACTION_LLM = os.getenv("COMPACTION_LLM") or EXTRACTION_LLM
# cheapest setting each provider accepts (gemini-3.8-flash rejects "minimal"); unlisted providers send none
REASONING_EFFORT_BY_PROVIDER = {"openai": "minimal", "gemini": "low"}
REASONING_EFFORT_OVERRIDE = os.getenv("LLM_REASONING_EFFORT")
MAX_HISTORY_MESSAGES = 20  # simple context guard - keep only the recent tail
MAX_OUTPUT_TOKENS = 500  # headroom for reasoning + a short reply - avoids empty responses
LLM_TIMEOUT_SECONDS = 30  # fail instead of hanging forever if the provider stalls
LLM_MAX_RETRIES = 1  # one retry for transient errors (timeouts, rate limits, 503s, etc.)

# User profile settings, read by profile_manager. Defaults match docs/profile_module_requirements.md.
PROFILE_EXTRACTION_ENABLED = _env_flag("PROFILE_EXTRACTION_ENABLED")  # off: nothing new is learned
PROFILE_COMPACTION_ENABLED = _env_flag("PROFILE_COMPACTION_ENABLED")  # off: categories may grow past limits
PROFILE_EXTRACTION_MAX_TOKENS = _env_int("PROFILE_EXTRACTION_MAX_TOKENS", MAX_OUTPUT_TOKENS)
PROFILE_COMPACTION_MAX_TOKENS = _env_int("PROFILE_COMPACTION_MAX_TOKENS", MAX_OUTPUT_TOKENS)
# overall cap on words injected into the prompt; the default is the sum of the category thresholds,
# so it only kicks in if compaction is off or failing
PROFILE_MAX_INJECT_WORDS = _env_int("PROFILE_MAX_INJECT_WORDS", 500)
# per category: (compact once active entries pass this many words, target words after compaction)
PROFILE_WORD_LIMITS = {
    category: (
        _env_int(f"PROFILE_THRESHOLD_{category.upper()}", threshold),
        _env_int(f"PROFILE_TARGET_{category.upper()}", target),
    )
    for category, threshold, target in [
        ("identity", 100, 60),
        ("ongoing_context", 200, 120),
        ("preference", 100, 60),
        ("instruction", 100, 60),
    ]
}


def reasoning_effort(model: str) -> str | None:
    return REASONING_EFFORT_OVERRIDE or REASONING_EFFORT_BY_PROVIDER.get(model.split("/")[0])

SYSTEM_PROMPT = {
    "role": "system",
    "content": "Respond in 1-3 sentences only. Be concise and direct.",
}

# the profile wins over SYSTEM_PROMPT where they conflict (e.g. a stored preference for detailed answers)
PROFILE_HEADER = (
    "\n\nWhat you know about this user. Where anything below conflicts with the "
    "instructions above, follow what's below:\n"
)

DRY_RUN_REPLY = "This is a canned dry-run reply - no LLM call was made."


class ChatRequest(BaseModel):
    session_id: str | None = None  # omit on the first message, we'll create one
    message: str
    dry_run: bool = False  # skip the real LLM call - zero cost, for testing the request/DB path


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    context: list[dict]


def response_usage(response, label: str) -> tuple[int, int, int | None, float | None]:
    """Logs token counts and returns (input_tokens, output_tokens, reasoning_tokens, cost_usd)."""
    usage = response.usage
    reasoning_tokens = None
    if usage.completion_tokens_details:
        reasoning_tokens = usage.completion_tokens_details.reasoning_tokens
    output_tokens = usage.completion_tokens - (reasoning_tokens or 0)

    try:
        cost_usd = completion_cost(completion_response=response)
    except Exception:
        cost_usd = None
        logging.warning(f"completion_cost failed for model={response.model}")

    logging.info(
        f"[tokens:{label}] input={usage.prompt_tokens} "
        f"thinking={reasoning_tokens or 0} "
        f"output={output_tokens} "
        f"total={usage.total_tokens}"
    )
    if response.choices[0].finish_reason == "length":
        logging.warning(f"[{label}] reply cut off at its max_tokens limit")
    return usage.prompt_tokens, output_tokens, reasoning_tokens, cost_usd


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, background_tasks: BackgroundTasks):
    session_id = req.session_id or str(uuid.uuid4())
    history = get_recent_messages(session_id, MAX_HISTORY_MESSAGES)

    history.append({"role": "user", "content": req.message})

    # Only send the recent tail to the model - keeps token usage and
    # context length bounded as a conversation grows.
    trimmed = history[-MAX_HISTORY_MESSAGES:]
    # one combined system message - safer across providers than two system-role entries
    system_content = SYSTEM_PROMPT["content"]
    profile_text = profile_manager.get_active_profile_text(DEFAULT_USER_ID)
    if profile_text:
        system_content += PROFILE_HEADER + profile_text
    messages = [{"role": "system", "content": system_content}] + trimmed

    if req.dry_run:
        reply = DRY_RUN_REPLY
        save_turn(
            user_id=DEFAULT_USER_ID,
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
            model=CHAT_LLM,
            messages=messages,
            reasoning_effort=reasoning_effort(CHAT_LLM),
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout=LLM_TIMEOUT_SECONDS,
            num_retries=LLM_MAX_RETRIES,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e}")
    latency_ms = int((time.monotonic() - start) * 1000)

    reply = response.choices[0].message.content or "(no response generated)"
    input_tokens, output_tokens, reasoning_tokens, cost_usd = response_usage(response, "chat")

    user_message_id = save_turn(
        user_id=DEFAULT_USER_ID,
        session_id=session_id,
        user_message=req.message,
        assistant_reply=reply,
        model=CHAT_LLM,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    # runs after the response is sent; dry runs never get here, so they never pay for extraction
    if PROFILE_EXTRACTION_ENABLED:
        background_tasks.add_task(
            profile_manager.process_message_for_profile,
            DEFAULT_USER_ID, session_id, user_message_id, req.message,
        )

    return ChatResponse(session_id=session_id, reply=reply, context=messages)


@app.get("/profile")
def get_profile():
    # no user_id parameter - once real users exist, the caller's id must come from their login
    return {"markdown": profile_manager.get_active_profile_markdown(DEFAULT_USER_ID)}


@app.get("/")
def root():
    return {"status": "ok"}
