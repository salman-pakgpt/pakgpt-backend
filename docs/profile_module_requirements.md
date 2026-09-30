# User Profile Module — Requirements

Status: approved 2026-10-01. The build plan and progress checklist are in [PRODUCT_SPEC.md §9](../PRODUCT_SPEC.md#9-next-steps-user-profile-module-before-phase-2).

## Approved changes to the original requirements

The review in PRODUCT_SPEC.md §9 found gaps in the original text below, and all of them were approved on 2026-10-01. **Where the original text conflicts with this section, this section wins.**

1. `save_turn` takes `user_id` and returns the user message's id, which `chat()` passes to extraction. It stays chat-only otherwise.
2. `dry_run` requests never schedule extraction. Profile injection still happens on dry runs, so it can be tested for free.
3. Every LLM operation is validated before it's applied:
   - `target_id` must be an active entry for this user and category.
   - `add` and `update` need `content`.
   - `update` and `delete` need `target_id`.

   Invalid operations are skipped and logged.
4. Compaction is all-or-nothing. Superseding the old entries and inserting the new ones happen in one transaction, and if the call fails or returns no entries, nothing changes.
5. `process_message_for_profile` runs under a module-level `threading.Lock`, so concurrent jobs can't race.
6. Schema conventions:
   - `profile.source_message_id` is `INTEGER`.
   - `created_at` and `updated_at` are `TEXT NOT NULL`, written from Python as ISO UTC like the other tables.
   - `updated_at` is set whenever the status changes.
7. `GET /profile` takes no `user_id` parameter and always uses `DEFAULT_USER_ID`.
8. The Streamlit console derives the `/profile` address from `API_URL` (which ends in `/chat`) and uses `layout="wide"`.
9. Structured output on Gemini is unproven here. Confirm it with the first real call, parse with `model_validate_json`, and log and drop a failed extraction rather than raising it. Check that thinking tokens plus JSON fit in `MAX_OUTPUT_TOKENS=500`.
10. `scripts/llm_usage.py` groups its by-model report by `call_type` as well, so chat and extraction/compaction costs stay separate.
11. Every chat message now costs two LLM calls instead of one, which halves the headroom on Gemini's free-tier rate limits. This is accepted.
12. **The profile wins over the fixed system prompt.** The injected text tells the model to follow the profile where the two conflict, for example "1-3 sentences" against a stored preference for detailed answers.
13. **Refresh (open item resolved):** option 1, re-fetching on every rerun, plus a manual "Refresh profile" button.
14. **Empty categories (open item resolved):** omit them. Show "No profile yet." only when the whole profile is empty.
15. Existing `messages` and `llm_calls` rows get `user_id = 'local_user'`. PRODUCT_SPEC.md §10 (Phase 2) is updated to match.
16. **Per-task models** replace "reuse `MODEL`": `CHAT_LLM`, `EXTRACTION_LLM` and `COMPACTION_LLM` can each be set on their own. Unset ones fall back in the order `COMPACTION_LLM` → `EXTRACTION_LLM` → `CHAT_LLM` → `LLM_MODEL`. Reasoning effort is derived from each model's own provider prefix.
17. **Configuration settings** (all optional, with defaults that keep the behavior described here):
    - `PROFILE_THRESHOLD_<CATEGORY>` and `PROFILE_TARGET_<CATEGORY>`: the category thresholds table.
    - `PROFILE_MAX_INJECT_WORDS` (default 500, the sum of the thresholds): entries that would push the injected text past the cap are left out, and a warning is logged.
    - `PROFILE_EXTRACTION_MAX_TOKENS` and `PROFILE_COMPACTION_MAX_TOKENS` (default 500).
    - `PROFILE_EXTRACTION_ENABLED` and `PROFILE_COMPACTION_ENABLED` (default on).

    These match the "Configuration" addendum at the end of this document, with one deliberate difference. When `PROFILE_MAX_INJECT_WORDS` would be exceeded, entries that don't fit are **left out whole** rather than the text being cut mid-sentence, because a half fact can change meaning. Later entries that still fit are kept, so short entries such as instructions survive one long entry. The cap is never exceeded, and a warning is still logged.
18. **`ProfileOperation.content` is required** (`str`, with an empty string for `delete`), not `Optional[str]`. With it optional, `gemini-3.5-flash-lite` returned an `update` with no content, and the correction was lost. Likewise, `ExtractionResult.operations` has no `= []` default. A default ends up in the JSON schema, which OpenAI's strict structured-output mode (used for `openai/` models) may reject.

---

## Before starting

1. **New file breaks the two-file convention.** `CLAUDE.md` says keep `main.py` + `streamlit.py` unless asked to split. This feature needs its own module (`profile_manager.py`, see below) — this is a deliberate, approved exception, not scope creep. Flag it in the PR description.
2. **This will exceed the ~50-line change threshold** the project convention calls out. Per that convention, post a short implementation plan back before writing the full diff.
3. **Async decision is made, not open:** use FastAPI `BackgroundTasks` for extraction. Rationale is in the relevant section below — raise it if there's disagreement before building.

---

## What this is

A per-user profile, separate from conversation history, holding four kinds of durable facts: `identity`, `ongoing_context`, `preference`, `instruction`. It's extracted from every user message, injected into the system prompt on every chat call, periodically compacted so it never grows unbounded, and shown live in the frontend.

Single hardcoded user for now (`DEFAULT_USER_ID = "local_user"`), structured so multiple users is a config/auth change later, not a schema change.

**Explicitly out of scope for this feature:** a `conversations` table, per-session ownership resolution, or generic "memory of everything said." Only the four categories above get written. Troubleshooting/debugging content and one-off questions are never extracted.

---

## Schema changes

Via `_ensure_column()` / a new `CREATE TABLE IF NOT EXISTS`, same pattern as the rest of `init_db()`.

```sql
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'local_user',
    category TEXT NOT NULL CHECK (category IN ('identity','ongoing_context','preference','instruction')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','deleted')),
    source_session_id TEXT,
    source_message_id TEXT,
    source_type TEXT NOT NULL DEFAULT 'extraction' CHECK (source_type IN ('extraction','compaction')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_profile_active ON profile(user_id, category, status);
```

Also via `_ensure_column`:
- `messages.user_id TEXT NOT NULL DEFAULT 'local_user'`
- `llm_calls.user_id TEXT NOT NULL DEFAULT 'local_user'`

No `conversations` table. Ownership is just a `user_id` column on existing rows, same pattern as the existing `session_id`.

---

## `call_type` values

`llm_calls.call_type` already exists as a column. Add two new values: `'extraction'`, `'compaction'`. The existing `save_turn` stays untouched and chat-only (`call_type='chat'`, always writes user+assistant message rows) — extraction/compaction never touch `messages`, only `profile` and `llm_calls`, so they need a separate, smaller writer.

---

## New module: `profile_manager.py`

Keep it self-contained. Public entry points `main.py` needs:

```python
def get_active_profile_text(user_id: str) -> str:
    """Returns the current profile formatted for system-prompt injection.
    Empty string if profile has no active rows."""

def get_active_profile_markdown(user_id: str) -> str:
    """Returns the current profile as a rendered markdown document, grouped
    by category under headers. Used by the /profile endpoint (see Frontend
    section below). Separate from get_active_profile_text because the
    prompt-injection format and the human-readable display format don't need
    to match."""

def process_message_for_profile(user_id: str, session_id: str, message_id: str, message_text: str) -> None:
    """Runs extraction, applies operations, triggers compaction if needed.
    This is the function passed to background_tasks.add_task()."""
```

Everything else (extraction call, compaction call, `apply_operations`, word-count thresholds) is internal to this file.

---

## Integration points in `main.py`

**1. `init_db()`** — add the `CREATE TABLE` and the two `_ensure_column` calls for `user_id`.

**2. System prompt assembly in `chat()`** — currently:
```python
messages = [SYSTEM_PROMPT] + trimmed
```
Change to build one combined system message (safer across providers than two separate system-role entries):
```python
profile_text = profile_manager.get_active_profile_text(DEFAULT_USER_ID)
system_content = SYSTEM_PROMPT["content"]
if profile_text:
    system_content += f"\n\nWhat you know about this user:\n{profile_text}"
system_message = {"role": "system", "content": system_content}
messages = [system_message] + trimmed
```

**3. After the reply is generated and `save_turn` runs**, add:
```python
background_tasks.add_task(
    profile_manager.process_message_for_profile,
    DEFAULT_USER_ID, session_id, user_message_id, user_message_text
)
```
Requires adding `background_tasks: BackgroundTasks` as a parameter to `chat()`.

**4. New endpoint:**
```python
@app.get("/profile")
def get_profile(user_id: str = DEFAULT_USER_ID):
    return {"markdown": profile_manager.get_active_profile_markdown(user_id)}
```

**5. `docker/api.Dockerfile`** — add `COPY profile_manager.py .` alongside the existing `main.py` copy line.

---

## Async decision — rationale

`BackgroundTasks`: no new dependency, ships with FastAPI, works with the existing sync `def chat()`. Trade-off accepted: a task is lost on container restart, no retry, and the profile used for injection can lag by one message. That's fine here — a stated fact doesn't need to affect the same turn it was stated in, only turns after. If that requirement changes later, switch to inline execution (remove `background_tasks.add_task`, call directly) — a one-line change.

---

## LLM calls — structured output

First structured-output call in the project. Use LiteLLM's `response_format` with Pydantic models:

```python
from pydantic import BaseModel
from typing import Literal, Optional

class ProfileOperation(BaseModel):
    action: Literal['add', 'update', 'delete']
    target_id: Optional[int] = None
    category: Literal['identity', 'ongoing_context', 'preference', 'instruction']
    content: Optional[str] = None

class ExtractionResult(BaseModel):
    has_update: bool
    operations: list[ProfileOperation] = []

class CompactionResult(BaseModel):
    entries: list[str]
```

**Model config:** reuse the existing `MODEL` env var for both extraction and compaction calls in v1 — no new config needed, and it avoids extending the reasoning-effort prefix table for a second model right now. Revisit with a dedicated `LLM_EXTRACT_MODEL` env var only if cost or quality becomes a real problem.

**Token budget:** both calls return small JSON — should fit inside `MAX_OUTPUT_TOKENS=500`, but confirm this in testing rather than assuming, especially for compaction on `ongoing_context` (the largest category).

---

## Extraction prompt (runs on every user message, via `BackgroundTasks`)

```
SYSTEM:
You are the profile-extraction step for a personal AI assistant. Look at the
user's latest message and decide whether it reveals anything durable about
who the user is, what they're actively working on, a preference for how you
should respond, or a standing instruction.

Categories:
- identity: stable facts — name, profession, company, location, background.
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

INPUT:
message: "{user_message}"
current_profile: {profile_grouped_by_category_with_ids}
```

## Compaction prompt (runs per category, only when that category crosses its word threshold)

```
SYSTEM:
You are compacting the "{category}" section of a user's living profile for
a personal AI assistant. This section has grown past its target size.
Rewrite its entries into a smaller set that preserves what's current and
meaningful. Nothing here is permanently protected — if something is stale,
redundant, or superseded by other entries, shorten, merge, or drop it
entirely. Aim for the result to total roughly {target_words} words.

INPUT:
current_entries: {list of {id, content} for this category only}
```

---

## Category thresholds

| Category | Compaction triggers at | Target after compaction |
|---|---:|---:|
| identity | 100 words | ~60 |
| ongoing_context | 200 words | ~120 |
| preference | 100 words | ~60 |
| instruction | 100 words | ~60 |

Checked per-category, only for categories touched by the current message's operations — not a full-profile scan on every turn.

---

## Orchestration logic (inside `profile_manager.py`)

```python
def process_message_for_profile(user_id, session_id, message_id, message_text):
    active_profile = get_active_profile(user_id)  # grouped by category, with ids
    result = call_extraction(message_text, active_profile)  # returns ExtractionResult

    if not result.has_update:
        return

    apply_operations(result.operations, user_id, session_id, message_id)

    touched_categories = {op.category for op in result.operations}
    for cat in touched_categories:
        if word_count(get_active_profile(user_id, category=cat)) > SOFT_THRESHOLD[cat]:
            compact_category(user_id, cat)

def compact_category(user_id, category):
    entries = get_active_profile(user_id, category=category)
    result = call_compaction(category, entries, target=TARGET_WORDS[category])  # CompactionResult
    mark_all_superseded(entries)
    for content in result.entries:
        insert_row(user_id, category, content, source_type='compaction')
```

`apply_operations` handles `add` (insert), `update` (mark `target_id` superseded, insert new row), `delete` (mark `target_id` status=`'deleted'`).

---

## Frontend: live profile pane (Streamlit)

**Requirement:** a pane on the right side of the screen showing the current user profile, in markdown, rendered — visible alongside the existing chat interface.

**Layout:**
```python
chat_col, profile_col = st.columns([3, 1])

with chat_col:
    # existing chat UI stays here, unchanged

with profile_col:
    st.subheader("User Profile")
    profile_md = fetch_profile()  # GET {API_URL}/profile
    st.markdown(profile_md)
```

**Data source:** the new `GET /profile` endpoint added above. The Streamlit app calls it and renders the returned `markdown` string with `st.markdown(...)`.

**Refresh behavior — two options, pick one for v1:**

1. **Rerun-triggered (simplest, no new dependency):** the profile pane re-fetches and re-renders every time Streamlit reruns the script, which already happens after every chat message is sent. No polling, no timer. This is "updates after each turn," not literally real-time.
2. **Timed auto-refresh (closer to real-time):** add the `streamlit-autorefresh` package and wrap the profile column in a short interval (e.g. every 3–5 seconds) so the pane updates even without a new chat message, in case compaction or a delayed background task changes something between turns.

Recommend starting with **option 1** — it's zero new dependencies and matches the actual write frequency (profile only changes in response to a message being processed, so there's nothing to catch between turns in normal use).

**Known lag, worth flagging to whoever tests this:** because extraction runs in `BackgroundTasks` *after* the chat reply is already returned, the profile pane will show the **pre-update** profile immediately after sending a message, and pick up the new fact only on the *next* rerun (e.g. after the next message, or on the next auto-refresh tick if option 2 is used). This is expected, not a bug — it's the same one-message lag accepted in the async decision above.

**Formatting expectation for `get_active_profile_markdown`:**
```markdown
## Identity
- Faisal is a Data Science & AI Team Lead at Data Pilot, Islamabad.

## Ongoing Context
- Building PakGPT, a Pakistan-first AI assistant.

## Preferences
- Prefers concise, direct answers.

## Instructions
- (none yet)
```
Categories with no active rows can either be omitted or shown with a placeholder line (`- (none yet)`) — pick whichever is less code; not a meaningful product decision either way.

---

## Open items to confirm with the team

Both are resolved; see approved changes 13 and 14 above.

- Streamlit polling approach (option 1 vs 2 above).
- Whether empty categories are omitted or placeholder-shown in the markdown output.

---

## Configuration (addendum, received 2026-10-01)

All settings are env vars. Defaults keep the behavior described above. As built, they are read in `main.py` and used by `profile_manager.py` as `main.X`.

Per-category thresholds and targets:
- `PROFILE_THRESHOLD_IDENTITY`, default 100 - word count that triggers compaction for identity
- `PROFILE_TARGET_IDENTITY`, default 60 - target word count after compacting identity
- `PROFILE_THRESHOLD_ONGOING_CONTEXT`, default 200 - word count that triggers compaction for ongoing_context
- `PROFILE_TARGET_ONGOING_CONTEXT`, default 120 - target word count after compacting ongoing_context
- `PROFILE_THRESHOLD_PREFERENCE`, default 100 - word count that triggers compaction for preference
- `PROFILE_TARGET_PREFERENCE`, default 60 - target word count after compacting preference
- `PROFILE_THRESHOLD_INSTRUCTION`, default 100 - word count that triggers compaction for instruction
- `PROFILE_TARGET_INSTRUCTION`, default 60 - target word count after compacting instruction

Overall safety cap:
- `PROFILE_MAX_INJECT_WORDS`, default 500 - hard cap on total injected profile text. If exceeded, truncate and log a warning rather than silently sending an oversized prompt. *(As built: truncation happens at entry boundaries, and entries that don't fit are left out whole. See approved change 17.)*

Token limits for the new LLM calls:
- `PROFILE_EXTRACTION_MAX_TOKENS`, default same as `MAX_OUTPUT_TOKENS` (500) - output token cap for the extraction call
- `PROFILE_COMPACTION_MAX_TOKENS`, default same as `MAX_OUTPUT_TOKENS` (500) - output token cap for the compaction call

Kill switches:
- `PROFILE_EXTRACTION_ENABLED`, default true - if false, `process_message_for_profile` returns immediately without calling the model
- `PROFILE_COMPACTION_ENABLED`, default true - if false, `compact_category` is never triggered regardless of word count
