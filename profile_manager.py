import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Literal

from litellm import completion
from pydantic import BaseModel

# Circular on purpose: main imports this module, and all config (DB_PATH, models, PROFILE_*
# settings) is read as main.X at call time, after main has finished loading and read .env.
import main

# Display order and headings; also the full set of valid categories.
CATEGORIES = {
    "identity": "Identity",
    "ongoing_context": "Ongoing Context",
    "preference": "Preferences",
    "instruction": "Instructions",
}

Category = Literal["identity", "ongoing_context", "preference", "instruction"]


class ProfileOperation(BaseModel):
    action: Literal["add", "update", "delete"]
    target_id: int | None = None
    category: Category
    # required (empty for delete): with it optional, gemini-3.5-flash-lite returned updates with no content
    content: str


class ExtractionResult(BaseModel):
    has_update: bool
    operations: list[ProfileOperation] = []


class CompactionResult(BaseModel):
    entries: list[str]


EXTRACTION_PROMPT = """You are the profile-extraction step for a personal AI assistant. Look at the
user's latest message and decide whether it reveals anything durable about
who the user is, what they're actively working on, a preference for how you
should respond, or a standing instruction.

Categories:
- identity: stable facts - name, profession, company, location, background.
- ongoing_context: a project, goal, or task the user is actively working on.
- preference: how the user wants the assistant to behave or respond.
- instruction: an explicit standing directive the user has given.

Do NOT extract: one-off questions, troubleshooting/debugging content, help
requests that don't reveal anything durable about the user, or anything the
user asks to be forgotten (extract that as a delete instead).

You are given the user's message and the current active profile, grouped by
category with IDs. If something contradicts or updates an existing entry,
return an "update" against that entry's ID rather than adding a duplicate.
If nothing in this message is profile-worthy, return has_update: false.

Every operation needs a category. "add" needs content; "update" needs the
target_id and the new content; "delete" needs the target_id. For update and
delete, use the category the target entry is listed under."""

COMPACTION_PROMPT = """You are compacting the "{category}" section of a user's living profile for
a personal AI assistant. This section has grown past its target size.
Rewrite its entries into a smaller set that preserves what's current and
meaningful. Nothing here is permanently protected - if something is stale,
redundant, or superseded by other entries, shorten, merge, or drop it
entirely. Aim for the result to total roughly {target_words} words."""

_lock = threading.Lock()  # one profile update at a time - concurrent jobs would race on the same entries


def get_active_profile(user_id: str) -> dict[str, list[tuple[int, str]]]:
    """Active entries grouped by category: {category: [(id, content), ...]}, oldest first."""
    conn = sqlite3.connect(main.DB_PATH)
    rows = conn.execute(
        "SELECT id, category, content FROM profile "
        "WHERE user_id = ? AND status = 'active' ORDER BY id",
        (user_id,),
    ).fetchall()
    conn.close()
    profile: dict[str, list[tuple[int, str]]] = {}
    for entry_id, category, content in rows:
        profile.setdefault(category, []).append((entry_id, content))
    return profile


def _render(user_id: str, heading: str, separator: str, max_words: int | None = None) -> str:
    """Entries in CATEGORIES order; with max_words, entries that would exceed it are left out."""
    profile = get_active_profile(user_id)
    sections, words, dropped = [], 0, 0
    for category, title in CATEGORIES.items():
        lines = []
        for _, content in profile.get(category, []):
            entry_words = len(content.split())
            if max_words is not None and words + entry_words > max_words:
                dropped += 1
                continue
            words += entry_words
            lines.append(f"- {content}")
        if lines:
            sections.append(heading.format(title) + "\n" + "\n".join(lines))
    if dropped:
        logging.warning(f"profile: {dropped} entries left out of the prompt by PROFILE_MAX_INJECT_WORDS={max_words}")
    return separator.join(sections)


def get_active_profile_text(user_id: str) -> str:
    """Profile formatted for system-prompt injection, capped at PROFILE_MAX_INJECT_WORDS;
    empty string if there are no active entries."""
    return _render(user_id, "{}:", "\n", main.PROFILE_MAX_INJECT_WORDS)


def get_active_profile_markdown(user_id: str) -> str:
    """Profile as markdown for GET /profile; empty categories are omitted."""
    return _render(user_id, "## {}", "\n\n") or "No profile yet."


def process_message_for_profile(user_id: str, session_id: str, message_id: int, message_text: str) -> None:
    """Runs extraction, applies operations, triggers compaction if needed.
    This is the function passed to background_tasks.add_task(); failures are logged, never raised."""
    try:
        with _lock:
            _update_profile(user_id, session_id, message_id, message_text)
    except Exception:
        logging.exception(f"profile update failed for message_id={message_id}")


def _update_profile(user_id: str, session_id: str, message_id: int, message_text: str) -> None:
    profile = get_active_profile(user_id)
    current = {category: [{"id": i, "content": c} for i, c in entries] for category, entries in profile.items()}
    result = _call_llm(
        user_id, session_id, message_id, "extraction",
        main.EXTRACTION_LLM, main.PROFILE_EXTRACTION_MAX_TOKENS, EXTRACTION_PROMPT,
        f"message: {json.dumps(message_text)}\ncurrent_profile: {json.dumps(current)}",
        ExtractionResult,
    )
    if not result.has_update:
        return

    touched = _apply_operations(result.operations, profile, user_id, session_id, message_id)
    if not main.PROFILE_COMPACTION_ENABLED:
        return
    profile = get_active_profile(user_id)
    for category in touched:
        entries = profile.get(category, [])
        limit, target_words = main.PROFILE_WORD_LIMITS[category]
        if sum(len(content.split()) for _, content in entries) > limit:
            _compact_category(user_id, session_id, message_id, category, entries, target_words)


def _apply_operations(
    operations: list[ProfileOperation], profile: dict, user_id: str, session_id: str, message_id: int
) -> set[str]:
    """Applies the valid operations in one transaction and returns the categories they changed.
    target_id must be an active entry of this user in the operation's category - the model can invent ids."""
    active = {entry_id: category for category, entries in profile.items() for entry_id, _ in entries}
    now = datetime.now(timezone.utc).isoformat()
    touched = set()
    conn = sqlite3.connect(main.DB_PATH)
    try:
        for op in operations:
            content = (op.content or "").strip()
            needs_target = op.action in ("update", "delete")
            if (needs_target and active.get(op.target_id) != op.category) or (op.action != "delete" and not content):
                logging.warning(f"profile: skipping invalid operation {op.model_dump()}")
                continue
            if needs_target:
                conn.execute(
                    "UPDATE profile SET status = ?, updated_at = ? WHERE id = ?",
                    ("superseded" if op.action == "update" else "deleted", now, op.target_id),
                )
                del active[op.target_id]  # an entry can only be retired once
            if op.action != "delete":
                _insert_entry(conn, user_id, op.category, content, session_id, message_id, "extraction", now)
            touched.add(op.category)
        conn.commit()
    finally:
        conn.close()
    return touched


def _compact_category(
    user_id: str, session_id: str, message_id: int, category: str, entries: list[tuple[int, str]], target_words: int
) -> None:
    """Replaces the category's entries with a shorter rewrite, all-or-nothing: a failed call or an
    empty result leaves the category untouched."""
    result = _call_llm(
        user_id, session_id, message_id, "compaction",
        main.COMPACTION_LLM, main.PROFILE_COMPACTION_MAX_TOKENS,
        COMPACTION_PROMPT.format(category=category, target_words=target_words),
        f"current_entries: {json.dumps([{'id': i, 'content': c} for i, c in entries])}",
        CompactionResult,
    )
    new_entries = [content.strip() for content in result.entries if content.strip()]
    if not new_entries:
        logging.warning(f"profile: compaction of {category} returned no entries - left unchanged")
        return

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(main.DB_PATH)
    try:
        conn.executemany(
            "UPDATE profile SET status = 'superseded', updated_at = ? WHERE id = ?",
            [(now, entry_id) for entry_id, _ in entries],
        )
        for content in new_entries:
            # source = the message whose extraction triggered this compaction
            _insert_entry(conn, user_id, category, content, session_id, message_id, "compaction", now)
        conn.commit()
    finally:
        conn.close()


def _insert_entry(
    conn: sqlite3.Connection, user_id: str, category: str, content: str,
    session_id: str, message_id: int, source_type: str, now: str,
) -> None:
    conn.execute(
        "INSERT INTO profile (user_id, category, content, source_session_id, source_message_id, "
        "source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, category, content, session_id, message_id, source_type, now, now),
    )


def _call_llm(
    user_id: str, session_id: str, message_id: int, call_type: str, model: str, max_tokens: int,
    system_prompt: str, user_content: str, schema: type[BaseModel],
):
    """One structured-output call, logged to llm_calls (linked to the user message) before parsing,
    so a reply that fails to parse still has its tokens recorded."""
    start = time.monotonic()
    response = completion(
        model=model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        response_format=schema,
        reasoning_effort=main.reasoning_effort(model),
        max_tokens=max_tokens,
        timeout=main.LLM_TIMEOUT_SECONDS,
        num_retries=main.LLM_MAX_RETRIES,
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    input_tokens, output_tokens, reasoning_tokens, cost_usd = main.response_usage(response, call_type)

    conn = sqlite3.connect(main.DB_PATH)
    conn.execute(
        """
        INSERT INTO llm_calls (
            user_id, session_id, message_id, call_type, model,
            input_tokens, output_tokens, reasoning_tokens,
            cost_usd, latency_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, session_id, message_id, call_type, model, input_tokens, output_tokens,
         reasoning_tokens, cost_usd, latency_ms, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    return schema.model_validate_json(response.choices[0].message.content or "")
