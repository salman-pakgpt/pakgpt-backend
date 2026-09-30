# PakGPT — Product Specification

**Last updated:** 2026-10-01 · **Code as of:** commit `69cebe6` · **Phase:** 1 complete, Phase 2 not started

This is the handover document. Together with access to the codebase, it should tell a new owner everything about where the product stands. Section 2 describes what exists today; sections 9 and 10 list what comes next, in order. Update both in the same change that ships something, and bump the date and commit above.

For setup commands see [README.md](README.md). For code-level detail aimed at AI coding agents see [CLAUDE.md](CLAUDE.md).

---

## 1. What PakGPT is

PakGPT is a chat assistant. A user types a message, the backend sends it to a large language model along with the recent conversation, and the reply comes back and is saved.

Today it is a working prototype used only by its owner, on one machine. The next goal is a private demo for 5–10 invited friends, which needs user accounts and a public HTTPS deployment (section 10). Before that, a per-user profile module is planned (section 9).

## 2. Current state at a glance

| Area | State today |
|---|---|
| Chat | Working end to end with a real model. Nothing is mocked. |
| Model | Chat on `gemini/gemini-3.8-flash`, and profile extraction and compaction on `gemini/gemini-3.5-flash-lite`, all on Google's free tier and called through LiteLLM. Each task's model is a one-line `.env` change. |
| Conversation memory | Last 20 messages of a conversation are sent with each request. |
| User profile | Durable facts about the user, in four categories, are pulled from each message in the background, added to every prompt, and compacted when a category grows too large. |
| Storage | SQLite: every message, one usage row per model call, and the user profile. |
| Usage tracking | Tokens, cost, and latency logged per call; CSV reports by session, model, and day. |
| Free testing | `dry_run` requests exercise the whole path without calling the model. |
| User interface | A Streamlit **test console** only, with a live profile pane. It is a developer tool, not the product UI. |
| Users and login | **None.** Anyone who can reach the API can chat. |
| Deployment | Local Docker Compose only. Both ports are bound to `127.0.0.1`. |
| Automated tests | None. |

## 3. How it works

```
Browser ──> Streamlit console (:8501) ──POST /chat──> FastAPI (:8000) ──LiteLLM──> Gemini
                                                          │
                                                          └──> SQLite (messages, llm_calls)
```

What happens on one `/chat` request (all in [main.py](main.py)):

1. The client sends a message and, after the first turn, the `session_id` it got back last time. If there is no `session_id`, the API creates one.
2. The API loads the last 20 messages for that `session_id` from SQLite.
3. It adds a fixed system prompt ("Respond in 1-3 sentences only…") and the new message.
4. It calls the model: 500-token output cap, 30-second timeout, 1 retry.
5. On success it saves the user message, the reply, and a usage row in one transaction. A failed call saves nothing and returns HTTP 502.
6. It returns the reply and the exact message list that was sent to the model.

**Two services, one repo.** The `api` container runs FastAPI; the `streamlit` container runs the test console and calls the API over the internal Docker network. SQLite lives in the `sessions-data` Docker volume.

## 4. API

`POST /chat`

| Field | Required | Meaning |
|---|---|---|
| `message` | yes | The user's text. |
| `session_id` | no | Conversation id. Leave it out to start a new conversation; send back the returned one to continue. |
| `dry_run` | no, default `false` | If `true`, skip the model and return a canned reply at zero cost. The rows are still saved, marked as test data. |

Response: `{ "session_id", "reply", "context" }`. `context` is the full message list sent to the model.

After each real (non-dry-run) reply, a background job makes one more LLM call to update the user profile. The reply doesn't wait for it.

`GET /profile` returns the user profile as markdown: `{ "markdown" }`. There is no user parameter; it always returns the single local user's profile.

`GET /` is a health check that returns `{"status": "ok"}`.

**Important:** `session_id` is only a conversation label, not proof of identity. Anyone who knows or guesses one can continue that conversation and read its last 20 messages through `context`. That's acceptable on localhost and must change before real users (section 10, workstream A).

## 5. Data model

**`messages`**: one row per chat message.

| Column | Notes |
|---|---|
| `id` | Auto-increment; also gives message order. |
| `session_id` | Conversation id. |
| `role` | `user` or `assistant`. |
| `content` | Message text. |
| `created_at` | UTC ISO timestamp. |
| `is_test` | `1` only for dry-run rows. |
| `user_id` | Always `local_user` until Phase 2 adds logins. |

**`llm_calls`**: one row per model call.

| Column | Notes |
|---|---|
| `session_id`, `message_id` | Links the call to its conversation. For `chat`, `message_id` is the assistant message it produced; for `extraction` and `compaction`, it is the user message being processed. |
| `call_type` | `chat`, `extraction` or `compaction`. |
| `user_id` | Always `local_user` until Phase 2 adds logins. |
| `model` | Model name, or `dry-run`. |
| `input_tokens`, `output_tokens`, `reasoning_tokens` | From the provider's usage data. |
| `cost_usd` | LiteLLM's price estimate. `NULL` if the model isn't in LiteLLM's price table. |
| `latency_ms` | Time spent in the model call only. |
| `created_at` | UTC ISO timestamp. |
| `is_test` | `1` only for dry-run rows. These are left out of reports. |

**`profile`**: one row per profile entry. Rows are never deleted, only re-marked.

| Column | Notes |
|---|---|
| `user_id` | Always `local_user` for now. |
| `category` | `identity`, `ongoing_context`, `preference` or `instruction`. |
| `content` | The fact. |
| `status` | `active` (used in prompts), `superseded` (replaced by an update or compaction) or `deleted` (the user asked to forget it). |
| `source_session_id`, `source_message_id` | The message that produced the entry; for compaction, the message that triggered it. |
| `source_type` | `extraction` or `compaction`. |
| `created_at`, `updated_at` | UTC ISO timestamps; `updated_at` changes whenever the status does. |

There is no `users` table yet. Tables are created, and new columns added, automatically when the API starts.

## 6. Configuration

All settings come from `.env`, which is never committed. Full table in [README.md](README.md#configuration).

- `GEMINI_API_KEY` (plus `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` if those providers are used).
- `CHAT_LLM`, `EXTRACTION_LLM`, `COMPACTION_LLM`: the model for each task, set as literal values. Currently chat is on `gemini/gemini-3.8-flash`, and extraction and compaction are on `gemini/gemini-3.5-flash-lite`. Unset ones fall back to the previous task's model, and finally to the legacy `LLM_MODEL`. Keep the `gemini/` prefix: without it LiteLLM goes to Vertex AI and needs full Google Cloud credentials. The `LLM_MODEL_1`/`LLM_MODEL_2` lines in `.env` are only a reference list of available models, and the app never reads them.
- The reasoning setting is chosen automatically from the provider (`gemini` → `low`, `openai` → `minimal`). `LLM_REASONING_EFFORT` overrides it but is normally unset.
- `DB_PATH` and `API_URL` point at the database file and the API.

## 7. Operating it

- **Run:** `docker compose up --build`, then open http://127.0.0.1:8501. Details and the non-Docker route are in the README.
- **Usage reports:** `docker compose exec api python scripts/llm_usage.py` writes the CSVs directly to `reports/` in the project folder, through a bind mount, so there's one copy and nothing to copy out. Older reports are in `reports/archive/`.
- **Docker only:** the app is run and tested only through Docker Compose. The live database is in the `sessions-data` volume. A leftover `sessions.db` in the project folder, from earlier venv runs, is kept as an archive but is no longer used.
- **Testing rules:**
  - Use `dry_run: true` whenever the model's actual answer doesn't matter.
  - Name real test conversations `test-<purpose>-<UTC timestamp>`.
  - Test data is never deleted automatically.

## 8. Known limitations

- **No authentication or per-user data.** See section 4; this is the main blocker for real users.
- **The console forgets on reload.** Its history lives in the browser session only, even though everything is in SQLite.
- **No usage caps or rate limiting.** One client can use up the free-tier quota.
- **Gemini's free tier allows 20 requests per day per model,** and each chat message now costs 2 or more because of profile extraction. Gemini also often returns 503 errors. See the §9 findings.
- **Gemini is slower.** In testing on 2026-09-27/28, replies averaged 2.6–4.8 s, against about 1 s for `openai/gpt-5-nano`.
- **`cost_usd` shows paid-tier prices.** On Gemini's free tier the actual bill is $0.
- **Old test traffic counts as real usage.** The `ctx-test` session (26 calls, 2026-09-26) predates the naming rule and the `is_test` flag.
- **All timestamps and report days are UTC.** That is 5 hours behind Pakistan time.
- **No automated tests and no backups.** SQLite is a single file in a Docker volume, which is fine at this scale.

## 9. Next steps: user profile module (before Phase 2)

**Goal:** the assistant keeps a short profile of durable facts about the user, uses it in every reply, stops it from growing without limit, and shows it live next to the chat.

For now there is one hardcoded user, `DEFAULT_USER_ID = "local_user"`. The design is set up so that more users later is an auth change, not a schema change. It is recommended to ship this before Phase 2, because it needs no logins and it adds the `user_id` columns that Phase 2 builds on.

**Out of scope:**
- a `conversations` table
- per-conversation ownership
- general memory of everything said

Only the four profile categories are ever stored. Troubleshooting content and one-off questions are never extracted.

### Agreed design

This is a summary. The full requirements, including the prompts, SQL, Pydantic models and the approved changes, are in [docs/profile_module_requirements.md](docs/profile_module_requirements.md).

- **New file, `profile_manager.py`.** This is an approved exception to the two-file rule; flag it in the PR description. It exposes three functions:
  - `get_active_profile_text(user_id)`: the profile formatted for the prompt
  - `get_active_profile_markdown(user_id)`: the profile formatted for display
  - `process_message_for_profile(user_id, session_id, message_id, message_text)`: the background job

  Everything else stays private to the file.
- **Data:**
  - a new `profile` table. Each entry has a category (`identity`, `ongoing_context`, `preference` or `instruction`), its text, and a status (`active`, `superseded` or `deleted`). It also records which session and message it came from, and whether extraction or compaction created it.
  - `user_id` is added to `messages` and `llm_calls`, defaulting to `local_user`.
- **Extraction:**
  - After every successful chat reply, a FastAPI `BackgroundTasks` job makes one LLM call on the user's message.
  - The call uses structured output: Pydantic models passed through LiteLLM's `response_format`. This is the first structured-output call in the project.
  - It returns `add`, `update` or `delete` operations against profile entry ids.
  - Each call is logged in `llm_calls` with `call_type = 'extraction'`.
- **Compaction:** when a category that the current message touched goes over its word limit, one LLM call rewrites that category. The old entries are marked `superseded`, and the call is logged with `call_type = 'compaction'`.

  | Category | Compact above | Target after |
  |---|---:|---:|
  | identity, preference, instruction | 100 words | ~60 |
  | ongoing_context | 200 words | ~120 |

- **Model:** each task has its own model setting (`CHAT_LLM`, `EXTRACTION_LLM`, `COMPACTION_LLM`), falling back to `LLM_MODEL`. This replaced the original "reuse `LLM_MODEL`" on 2026-10-01.
- **Prompt:** the profile is appended to the single system message under "What you know about this user:".
- **Frontend:** a pane on the right of the Streamlit console renders the profile from a new `GET /profile` endpoint.
- **Accepted trade-offs:**
  - A background job is lost if the container restarts, and there is no retry.
  - The profile lags one message: a fact stated now shows up in the prompt from the next turn.

### Gaps found in review (resolve while building)

1. **`save_turn` has to change a little.** The requirements say it stays untouched, but `chat()` needs the user message's id to pass to extraction, and `save_turn` doesn't return it. Return that id, and pass `user_id` explicitly rather than relying on the column default, so that multi-user later doesn't need a code change here.
2. **Dry runs must not trigger extraction.** Extraction is a real, paid LLM call, and `dry_run` promises zero cost. Profile injection should still happen in dry runs, which makes injection testable for free through `context`.
3. **Validate every operation from the LLM before applying it:**
   - `target_id` must be an active entry of this user and category.
   - `add` and `update` need `content`.
   - `update` and `delete` need `target_id`.

   Skip and log anything invalid, since the model can invent ids.
4. **Make compaction all-or-nothing.** Mark the old entries superseded and insert the new ones in one transaction. If the call fails or returns no entries, change nothing. Otherwise a failure could wipe a whole category.
5. **Serialize background jobs with a module-level `threading.Lock`.** Two quick messages run two jobs at once, which can add duplicate entries or run overlapping compactions.
6. **Match the existing schema conventions:**
   - `source_message_id` should be `INTEGER`, since `messages.id` is an integer.
   - Write `created_at` and `updated_at` from Python as ISO UTC like the other tables, not with SQLite's `CURRENT_TIMESTAMP`.
   - Set `updated_at` whenever the status changes.
7. **Drop the `user_id` query parameter from `GET /profile`** and always use `DEFAULT_USER_ID`. Otherwise, once real users exist, any caller could read any profile.
8. **Streamlit needs the base API address.** `API_URL` ends in `/chat`, so derive the `/profile` address from it. Also switch the page to `layout="wide"`, or the `[3, 1]` columns will be cramped.
9. **Structured output on Gemini is unproven here.**
   - Gemini's schema support may not handle `Optional` fields or defaults.
   - Confirm with the first real call.
   - Parse replies with `model_validate_json`.
   - A failed extraction is logged and dropped, never raised.
   - Check that thinking tokens plus JSON fit in `MAX_OUTPUT_TOKENS=500`.
10. **Every message now costs two LLM calls instead of one.** That halves the headroom on Gemini's free-tier rate limits.
11. **Reports would mix call types.** `scripts/llm_usage.py` would combine chat and extraction calls. Group the by-model report by `call_type` as well.
12. **Open product question: which instruction wins?** The fixed prompt says "1-3 sentences", but a stored preference might ask for detailed answers. The recommendation is that the profile overrides the base prompt, stated in the injected text.
13. **Existing rows will get `user_id = 'local_user'`,** not empty as Phase 2 originally said. They belong to the owner, so this is fine; §10 is updated to match.

**Recommended answers to the requirements' open items:**
- **Refresh:** option 1 (the pane re-fetches on every rerun), plus a "Refresh profile" button. A button click reruns the script, which works around the one-message lag with no new dependency.
- **Empty categories:** omit them. If the whole profile is empty, show "No profile yet."

### Action items (in this order)

**Progress (2026-10-01): done and verified on real output.** Every rule was checked for free with mocked LLM replies: 18 profile checks and 21 configuration checks. On real Gemini output:
- injection, adding facts, and ignoring a troubleshooting question work on `gemini-3.8-flash`
- corrections, compaction, and forgetting work on `gemini-3.5-flash-lite`

**Real-output results:**
- **Correction:** "I moved to Lahore" produced an `update` that superseded the Karachi entry, with no duplicate.
- **Compaction:** it took a 110-word identity section down to 58 words (target 60) in 94 output tokens, and marked the old entries `superseded`.
- **Forget:** the fact had already been merged into a compacted entry, so the model rewrote that entry without the fact (an `update`). It didn't `delete` the whole entry, which would also have lost the name and location.
- **Schema fix:** `ProfileOperation.content` is now a required field. With it optional, flash-lite returned an `update` with no content, which was skipped, and the correction was lost.

**Per-task models and settings (added 2026-10-01):**
- `CHAT_LLM`, `EXTRACTION_LLM` and `COMPACTION_LLM` can each be set on their own. Unset ones fall back in the order `COMPACTION_LLM` → `EXTRACTION_LLM` → `CHAT_LLM` → `LLM_MODEL`.
- There are `PROFILE_*` settings for thresholds, targets, token caps, the word cap on the injected profile, and two kill switches.
- The defaults keep behavior unchanged (see the README configuration table).
- The local `.env` now runs extraction and compaction on `gemini/gemini-3.5-flash-lite`, and chat on `gemini-3.8-flash`. Flash-lite calls took 1–2 s with no 503s, compared with 6–9 s on `gemini-3.8-flash`.

**Findings from real testing (2026-10-01):**
- **The free tier allows only 20 requests per day per model** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, value 20, for `gemini-3.8-flash`). Each chat message now uses at least 2 of them, a compaction adds 1, and failed attempts and retries count too. That leaves roughly 8–10 messages a day for everyone combined. **This blocks Phase 2 on the free tier**, and it needs a decision (see §10).
- **Gemini often returned 503 "Service Unavailable".** Of 5 first attempts at extraction, 3 failed. The single LiteLLM retry doesn't always help, and a failed extraction is lost by design, so some facts never reach the profile.
- **Structured output works on Gemini.** The Pydantic schemas passed through `response_format` were accepted. Extraction used 18–117 output tokens and up to 59 thinking tokens, well under the 500 cap.
- **The model can return invalid ids.** One extraction returned an 80-digit `target_id` with no content. The operation check skipped it, as designed. The trigger was a degenerate test row (86 repeated words), not realistic data.

Items 1–6 can be tested for free: seed `profile` rows with SQL and use `dry_run`. Items 8–9 need about 4 real LLM calls. Use session ids of the form `test-profile-<UTC timestamp>`.

- [x] **1. Schema:** in `init_db()`, add the `profile` table and index, and use `_ensure_column` for `user_id` on `messages` and `llm_calls`. Verify after `docker compose up --build` that the columns exist and old rows read `local_user`.
- [x] **2. `save_turn`:** take `user_id` and return the user message's id (gap 1).
- [x] **3. `profile_manager.py`, read side:**
  - a grouped active-profile query that includes ids
  - `get_active_profile_text` and `get_active_profile_markdown`
  - `COPY profile_manager.py .` in `docker/api.Dockerfile`
- [x] **4. Prompt injection in `chat()`:** build one combined system message. Verify for free by seeding rows and making a dry run, then check `context`.
- [x] **5. `GET /profile`:** always uses the default user (gap 7). Verify with the seeded rows.
- [x] **6. Streamlit profile pane:**
  - wide layout with `[3, 1]` columns
  - fetch `/profile` from the address derived from `API_URL`
  - add the refresh button

  Verify with the seeded rows.
- [x] **7. `llm_calls` writer for background calls:** a small function that writes only to `llm_calls`, with `call_type` set to `extraction` or `compaction` and `message_id` pointing at the user message. Shared by items 8 and 9.
- [x] **8. Extraction:**
  - Pydantic models, prompt and call
  - `apply_operations` with validation (gap 3)
  - the lock (gap 5)
  - wire `background_tasks.add_task` into `chat()` after `save_turn`, skipped for dry runs (gap 2)

  Verify with 2 real messages: one that states a durable fact, and one troubleshooting question that should add nothing.
- [x] **9. Compaction:** word counts, thresholds, and the all-or-nothing rewrite (gap 4). Verify by seeding a category just under its limit, then sending 1 real message that pushes it over. Check the new entries, the superseded old ones, and output tokens against the 500 cap.
- [x] **10. Reports:** add `call_type` to the by-model report (gap 11).
- [x] **11. Docs:**
  - `CLAUDE.md`: file structure, the profile flow, and testing notes
  - `README.md`: `GET /profile` and the console pane
  - this spec: §2–5, the decision log, and ticking these items

### Done when

- [x] A durable fact stated in chat appears in the profile pane (after a refresh) and in the system prompt of the next reply.
- [x] Correcting a fact updates the existing entry instead of duplicating it, and asking the assistant to forget something removes it: a `delete`, or an `update` if the fact has been merged into a compacted entry.
- [x] One-off and troubleshooting messages add nothing.
- [x] A category over its limit is compacted to about its target size, with the old entries kept as `superseded`.
- [x] A dry run makes no LLM calls and still shows the profile in `context`.
- [x] Extraction and compaction calls appear in `llm_calls` with their `call_type`.

**Size estimate:**
- `profile_manager.py`: about 170 lines, of which about 45 are prompts
- `main.py`: about 25 lines
- `streamlit.py`: about 15 lines
- Dockerfile and report script: a few lines
- docs: about 40 lines

This is well over the 50-line threshold, so this plan is the required write-up before implementation.

## 10. Next steps: Phase 2, private demo for 5–10 friends

**Goal:** a small invited group can sign in over HTTPS, chat with their own private history, and stay within a daily limit.

The two workstreams can run in either order or in parallel, with one constraint: **don't make the app public until workstream A ships.** Without it, anyone with the URL can chat, read other conversations, and read the user profile. If deployment happens first, put a temporary password on the whole site in Caddy (its `basic_auth` directive).

**Blocker: model quota.** Gemini's free tier allows 20 requests a day per model, and each message uses at least 2 once the profile module is on.
- **Partly addressed:** chat, extraction and compaction can now each run on their own model (`CHAT_LLM`, `EXTRACTION_LLM`, `COMPACTION_LLM`; see §9). Since the quota is per model, that spreads the load.
- **Still not enough for 5–10 users:** the chat model alone would allow about 20 messages a day for everyone combined.
- **Still to decide before Phase 2 ships:** move chat to Gemini's paid tier or another provider.

### Workstream A: accounts and data isolation

- [ ] **`users` table:** `id`, `name`, `phone`, `email`, `password_hash`, `status` (`invited` / `active`).
- [ ] **Pre-seed invited users** from the owner's list of 5–10 name/phone/email entries. Each gets a one-time invite code, not a password.
- [ ] **First login:** the user enters the invite code. If their status is `invited`, show a profile form with name, phone, and email pre-filled and editable, and have them set a password. Then mark them `active`, invalidate the code, and start a login session.
- [ ] **Use `streamlit-authenticator`** for the login cookie and password hashing rather than hand-rolling them. This is a new dependency, approved as part of this plan.
- [ ] **Map `local_user` to the owner's account.** The profile module (§9) adds `user_id` to `messages`, `llm_calls` and `profile`, with every existing row set to `local_user`. Phase 2 assigns those rows to the owner and takes each new request's `user_id` from the login instead of `DEFAULT_USER_ID`.
- [ ] **`/chat` requires a logged-in user.** It rejects requests without a valid login and takes `user_id` from the login, never from anything the client sends. A `session_id` alone must never grant access, and a user can only continue their own conversations. `GET /profile` gets the same check.
- [ ] **"My chat history" view in Streamlit:** after logging back in, a user sees their own past conversations, read from SQLite, and can reopen one.
- [ ] **Per-user daily message cap:** a config value, checked before calling the model.
- Out of scope: password reset. A forgotten password is fixed by hand for now.

**Decisions to settle before building:**

1. **How the API learns who the user is.** `streamlit-authenticator` handles login inside the Streamlit app, but `/chat` is a separate FastAPI service and can't read that login directly. Options:
   - (a) Make the API reachable only from the Streamlit container, never publicly, and have Streamlit pass the logged-in `user_id` with a shared secret.
   - (b) Have the API issue and check its own signed tokens.

   Recommendation: (a). It's the smaller change, and the deployment only exposes Streamlit anyway.
2. **Where credentials live.** `streamlit-authenticator` normally reads users from a config file. Here they must come from, and be saved back to, the `users` table. Also check how much of the invite-code step the library covers; it may need to be a small custom step before the library takes over.
3. **Naming.** "Session" would mean two things: a conversation (`session_id`) and a login. Pick one word for each in code and UI to avoid confusion.
4. **Daily cap details.** Decide:
   - whose midnight counts: UTC or Pakistan time
   - whether dry runs count
   - the default limit
   - what the user sees when capped, e.g. HTTP 429 with a friendly message
5. **Invite codes.** Store them hashed, like passwords, and decide whether they expire.

### Workstream B: deployment

- [ ] **Run the existing Docker Compose stack as-is** on the Oracle Cloud free-tier VM (the owner provides access).
- [ ] **Add Caddy as a reverse proxy** for automatic HTTPS. No app changes are needed for this.
- [ ] **Gemini on the VM:** create `.env` there with the free-tier `GEMINI_API_KEY` and `CHAT_LLM` / `EXTRACTION_LLM` / `COMPACTION_LLM`. The code already defaults to Gemini, so this is configuration only.

**Things to know:**

- **A domain name is needed.** Caddy's automatic HTTPS gets certificates for a domain pointed at the VM. Certificates for a bare IP address are not the standard route.
- **Only Caddy should be public.** Publish ports 80 and 443 and route to the Streamlit container. Leave 8000 (API) and 8501 (console) unpublished, reachable only inside the Docker network. Caddy passes Streamlit's websocket connection through without extra setup.
- **Oracle blocks ports in two places.** Open 80/443 both in the VCN security list and in the VM's own firewall; Oracle's stock images ship with restrictive firewall rules.
- **Check the chip type.** If the VM is an Ampere (ARM) shape, confirm the images build there. The `python:3.11-slim` base supports ARM, but every Python dependency needs an ARM build too.
- **The server's database starts empty.** Local history is not carried over. Backups are not yet planned.

### Phase 2 is done when

- [ ] Every invited friend can activate with their code, set a password, chat, log out, log back in, and see their past conversations.
- [ ] User A cannot see or continue user B's conversations, even when sending B's `session_id`.
- [ ] `/chat` without a valid login is rejected.
- [ ] Going over the daily cap is blocked before any model call.
- [ ] The app loads at `https://<domain>` with a valid certificate, and ports 8000/8501 are not reachable from the internet.

## 11. Later (not scheduled)

- Password reset flow
- Automated tests
- Database backups on the server

## 12. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-09-20 | FastAPI backend + Streamlit test console, called through LiteLLM | LiteLLM puts every provider behind one call, so switching provider is a config change. |
| 2026-09-27 | Docker Compose for both services; one row per message instead of one blob per conversation | Reproducible setup; history can be queried and trimmed without loading whole conversations. |
| 2026-09-27 | Log tokens, cost, and latency per call, with CSV reports | Make spend and speed visible per session, model, and day. |
| 2026-09-27 | `dry_run` flag and `is_test` column | Testing was spending real money and mixing test traffic into usage numbers. |
| 2026-09-28 | Default to Gemini (`gemini-3.8-flash` in `.env`) | Free tier for the demo. `gemini-2.5-flash-lite` is closed to new users. |
| 2026-09-28 | Reasoning setting chosen per provider | `gemini-3.8-flash` rejects the OpenAI-style `minimal` setting; switching `LLM_MODEL` should be the only change needed. |
| 2026-09-28 | Added `tenacity` | Without it, LiteLLM's retry never ran, and failures showed a misleading error. |
| 2026-10-01 | Docker Compose is the only supported way to run and test | One environment to reason about. Removed the venv-only `requirements.txt` and the already-applied `migrate_sessions.py`. |
| 2026-10-01 | User profile module goes in its own file, `profile_manager.py` | An approved exception to the two-file rule. It keeps `main.py` focused on the chat endpoint. |
| 2026-10-01 | Profile extraction runs in FastAPI `BackgroundTasks` | No new dependency, and the reply isn't delayed. Accepted in return: jobs are lost on restart, there is no retry, and the profile lags one message. |
| 2026-10-01 | Extraction and compaction reuse `LLM_MODEL` | No new config for v1. Superseded the same day by the row below. |
| 2026-10-01 | One model setting per task (`CHAT_LLM`, `EXTRACTION_LLM`, `COMPACTION_LLM`) with a fallback chain | Free-tier quota is per model, so splitting tasks spreads it. Leaving the settings unset changes nothing. Also allows compaction on a third, lighter model later. |
| 2026-10-01 | `PROFILE_MAX_INJECT_WORDS` leaves out whole entries rather than cutting the text | A fact cut mid-sentence can change meaning. The cap and the warning still hold, as the addendum asks. |
| 2026-10-01 | `ProfileOperation.content` is required | With it optional, `gemini-3.5-flash-lite` returned an `update` with no content, and the correction was lost. |
