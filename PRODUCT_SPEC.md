# PakGPT — Product Specification

**Last updated:** 2026-09-28 · **Code as of:** commit `9008859` · **Phase:** 1 complete, Phase 2 not started

This is the handover document. Together with access to the codebase, it should tell a new owner everything about where the product stands. Section 2 describes what exists today; section 9 lists what comes next. Update both in the same change that ships something, and bump the date and commit above.

For setup commands see [README.md](README.md). For code-level detail aimed at AI coding agents see [CLAUDE.md](CLAUDE.md).

---

## 1. What PakGPT is

PakGPT is a chat assistant. A user types a message, the backend sends it to a large language model along with the recent conversation, and the reply comes back and is saved.

Today it is a working prototype used only by its owner, on one machine. The next goal is a private demo for 5–10 invited friends, which needs user accounts and a public HTTPS deployment (section 9).

## 2. Current state at a glance

| Area | State today |
|---|---|
| Chat | Working end to end with a real model. Nothing is mocked. |
| Model | `gemini/gemini-3.8-flash` on Google's free tier, called through LiteLLM. Switching provider is a one-line `.env` change. |
| Conversation memory | Last 20 messages of a conversation are sent with each request. |
| Storage | SQLite: every message, plus one usage row per model call. |
| Usage tracking | Tokens, cost, and latency logged per call; CSV reports by session, model, and day. |
| Free testing | `dry_run` requests exercise the whole path without calling the model. |
| User interface | A Streamlit **test console** only. It is a developer tool, not the product UI. |
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

`GET /` is a health check that returns `{"status": "ok"}`.

**Important:** `session_id` is only a conversation label, not proof of identity. Anyone who knows or guesses one can continue that conversation and read its last 20 messages through `context`. That's acceptable on localhost and must change before real users (section 9, workstream A).

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

**`llm_calls`**: one row per model call.

| Column | Notes |
|---|---|
| `session_id`, `message_id` | Links the call to its conversation and to the assistant message it produced. |
| `call_type` | Always `chat` today. |
| `model` | Model name, or `dry-run`. |
| `input_tokens`, `output_tokens`, `reasoning_tokens` | From the provider's usage data. |
| `cost_usd` | LiteLLM's price estimate. `NULL` if the model isn't in LiteLLM's price table. |
| `latency_ms` | Time spent in the model call only. |
| `created_at` | UTC ISO timestamp. |
| `is_test` | `1` only for dry-run rows. These are left out of reports. |

There is no `users` table yet. Tables are created, and new columns added, automatically when the API starts.

## 6. Configuration

All settings come from `.env`, which is never committed. Full table in [README.md](README.md#configuration).

- `GEMINI_API_KEY` (plus `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` if those providers are used).
- `LLM_MODEL`: the only setting needed to switch models. Currently `gemini/gemini-3.8-flash`. Keep the `gemini/` prefix: without it LiteLLM goes to Vertex AI and needs full Google Cloud credentials.
- The reasoning setting is chosen automatically from the provider (`gemini` → `low`, `openai` → `minimal`). `LLM_REASONING_EFFORT` overrides it but is normally unset.
- `DB_PATH` and `API_URL` point at the database file and the API.

## 7. Operating it

- **Run:** `docker compose up --build`, then open http://127.0.0.1:8501. Details and the non-Docker route are in the README.
- **Usage reports:** `docker compose exec api python scripts/llm_usage.py` writes CSVs inside the container; copy them out with `docker compose cp`.
- **Two databases:** running from the venv uses `sessions.db` in the project folder, while Docker uses its own database in the `sessions-data` volume. They don't sync. The Docker one is the one that matters.
- **Testing rules:**
  - Use `dry_run: true` whenever the model's actual answer doesn't matter.
  - Name real test conversations `test-<purpose>-<UTC timestamp>`.
  - Test data is never deleted automatically.

## 8. Known limitations

- **No authentication or per-user data.** See section 4; this is the main blocker for real users.
- **The console forgets on reload.** Its history lives in the browser session only, even though everything is in SQLite.
- **No usage caps or rate limiting.** One client can use up the free-tier quota.
- **Gemini is slower.** In testing on 2026-09-27/28, replies averaged 2.6–4.8 s, against about 1 s for `openai/gpt-5-nano`.
- **`cost_usd` shows paid-tier prices.** On Gemini's free tier the actual bill is $0.
- **Old test traffic counts as real usage.** The `ctx-test` session (26 calls, 2026-09-26) predates the naming rule and the `is_test` flag.
- **All timestamps and report days are UTC.** That is 5 hours behind Pakistan time.
- **No automated tests and no backups.** SQLite is a single file in a Docker volume, which is fine at this scale.

## 9. Next steps: Phase 2, private demo for 5–10 friends

**Goal:** a small invited group can sign in over HTTPS, chat with their own private history, and stay within a daily limit.

The two workstreams can run in either order or in parallel, with one constraint: **don't make the app public until workstream A ships.** Without it, anyone with the URL can chat and read other conversations. If deployment happens first, put a temporary password on the whole site in Caddy (its `basic_auth` directive).

### Workstream A: accounts and data isolation

- [ ] **`users` table:** `id`, `name`, `phone`, `email`, `password_hash`, `status` (`invited` / `active`).
- [ ] **Pre-seed invited users** from the owner's list of 5–10 name/phone/email entries. Each gets a one-time invite code, not a password.
- [ ] **First login:** the user enters the invite code. If their status is `invited`, show a profile form with name, phone, and email pre-filled and editable, and have them set a password. Then mark them `active`, invalidate the code, and start a login session.
- [ ] **Use `streamlit-authenticator`** for the login cookie and password hashing rather than hand-rolling them. This is a new dependency, approved as part of this plan.
- [ ] **Add `user_id` to `messages` and `llm_calls`.** Rows written before this change keep `user_id` empty.
- [ ] **`/chat` requires a logged-in user.** It rejects requests without a valid login and takes `user_id` from the login, never from anything the client sends. A `session_id` alone must never grant access, and a user can only continue their own conversations.
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
- [ ] **Gemini on the VM:** create `.env` there with the free-tier `GEMINI_API_KEY` and `LLM_MODEL`. The code already defaults to Gemini, so this is configuration only.

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

## 10. Later (not scheduled)

- Password reset flow
- Automated tests
- Database backups on the server

## 11. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-09-20 | FastAPI backend + Streamlit test console, called through LiteLLM | LiteLLM puts every provider behind one call, so switching provider is a config change. |
| 2026-09-27 | Docker Compose for both services; one row per message instead of one blob per conversation | Reproducible setup; history can be queried and trimmed without loading whole conversations. |
| 2026-09-27 | Log tokens, cost, and latency per call, with CSV reports | Make spend and speed visible per session, model, and day. |
| 2026-09-27 | `dry_run` flag and `is_test` column | Testing was spending real money and mixing test traffic into usage numbers. |
| 2026-09-28 | Default to Gemini (`gemini-3.8-flash` in `.env`) | Free tier for the demo. `gemini-2.5-flash-lite` is closed to new users. |
| 2026-09-28 | Reasoning setting chosen per provider | `gemini-3.8-flash` rejects the OpenAI-style `minimal` setting; switching `LLM_MODEL` should be the only change needed. |
| 2026-09-28 | Added `tenacity` | Without it, LiteLLM's retry never ran, and failures showed a misleading error. |
