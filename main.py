import os
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel
from litellm import completion

load_dotenv()

app = FastAPI()

# In-memory session store: {session_id: [messages]}
# Resets on every restart/redeploy - fine while testing, swap for a
# Supabase table later when sessions need to survive a deploy.
sessions: dict[str, list[dict]] = {}

MODEL = os.getenv("LLM_MODEL", "openai/gpt-5-nano")
MAX_HISTORY_MESSAGES = 20  # simple context guard - keep only the recent tail
MAX_OUTPUT_TOKENS = 300  # headroom for reasoning + a short reply - avoids empty responses

SYSTEM_PROMPT = {
    "role": "system",
    "content": "Respond in 1-3 sentences only. Be concise and direct.",
}


class ChatRequest(BaseModel):
    session_id: str | None = None  # omit on the first message, we'll create one
    message: str


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    context: list[dict]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    history = sessions.setdefault(session_id, [])

    history.append({"role": "user", "content": req.message})

    # Only send the recent tail to the model - keeps token usage and
    # context length bounded as a conversation grows.
    trimmed = history[-MAX_HISTORY_MESSAGES:]
    messages = [SYSTEM_PROMPT] + trimmed

    response = completion(
        model=MODEL,
        messages=messages,
        reasoning_effort="minimal",
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    reply = response.choices[0].message.content

    usage = response.usage
    reasoning_tokens = 0
    if usage.completion_tokens_details:
        reasoning_tokens = usage.completion_tokens_details.reasoning_tokens or 0
    output_tokens = usage.completion_tokens - reasoning_tokens

    print(
        f"[tokens] input={usage.prompt_tokens} "
        f"thinking={reasoning_tokens} "
        f"output={output_tokens} "
        f"total={usage.total_tokens}"
    )

    history.append({"role": "assistant", "content": reply})

    return ChatResponse(session_id=session_id, reply=reply, context=messages)


@app.get("/")
def root():
    return {"status": "ok"}