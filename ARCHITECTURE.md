# Cricway Bot v2 — Production Architecture

A drop-in replacement for the v1 bot. Same DB schema, same handlers, same
Render deployment — but every hot path is now cached or non-blocking.

---

## Final Flow

### Static requests (Home, Support, AI, Safety, FAQ)

```
User taps button
   │
   ▼
query.answer()                         ← stops the spinner
   │
   ▼
STATIC_SCREENS[name]                   ← in-memory dict lookup (<1µs)
   │
   ▼
edit_message_media (file_id reuse)     ← single Telegram API call
   │
   ▼
asyncio.create_task(upsert_user)       ← DB write happens AFTER reply
```

**Total budget:** ~1 round-trip to Telegram (~150–300 ms). No DB, no recompute.

### Dynamic requests (free-form message)

```
User sends "I can't log in"
   │
   ▼
spam check (in-memory)
   │
   ▼
upsert_user + create_ticket            ← ~2 ms SQLite UPSERT + INSERT
   │
   ▼
faq_match (in-memory keyword index)
   │ ┌── hit ──► reply with answer + fire-and-forget DB writes
   │ │
   └─┴── miss ─► reply "⏳ Processing… CRIC-1042"     ← INSTANT ACK
                                  │
                                  ▼
                  asyncio.create_task(_process_ai_in_background)
                                  │
                                  ▼
                  await aget_ai_response(text)
                    │
                    ├── cache hit (< 5 min) ─► return cached
                    └── cache miss          ─► AsyncOpenAI call (dedup'd)
                                  │
                                  ▼
                  edit_message_text(ack_message_id, final_answer)
```

The user sees a ticket ID inside ~300 ms, every time. The AI answer
arrives later by editing the same message — no extra ping, no duplicate
"please wait" messages, no orphan messages.

---

## What Changed, by File

| File | Change | Why |
|------|--------|-----|
| `cache.py` | NEW. `STATIC_SCREENS`, `PHOTO_FILE_IDS`, `TTLCache`, `InflightDedupe` | Single source of truth for caching primitives |
| `ui.py` | Photo `file_id` cache; `edit_message_media`/`edit_message_text` instead of delete+send | Saves 1–2 round-trips per navigation; first-upload-only for assets |
| `ai.py` | `AsyncOpenAI`; 5-min response cache; in-flight dedupe | AI never blocks the loop; identical prompts → 1 upstream call |
| `database.py` | Single shared connection; WAL/MMAP/64MB cache PRAGMAs; queued `log_event`; `arun()` helper | DB ops faster + offloadable to thread pool |
| `tickets.py` | Per-user 10-second list cache; auto-invalidation on writes | "My Tickets" tab opens instantly after the first view |
| `bot.py` | Static cached screens; instant ACK + background AI; routes dispatch table; `concurrent_updates=True` | Implements every goal in your brief |
| `cricket_api.py` | NEW. TTL cache + dedupe + stale-fallback for any cricket endpoint | Drop-in pattern for when you wire in cricket data |
| `admin.py` | Broadcast now runs as a background task; cache-friendly | Confirm button returns immediately |
| `requirements.txt` | Bumped PTB to 21.6, openai >= 1.40 | AsyncOpenAI + concurrent_updates support |

---

## Caching Layer at a Glance

| Cache | TTL | Capacity | Hit Pattern |
|-------|-----|----------|-------------|
| `STATIC_SCREENS`   | ∞ (rebuild on restart) | 4 entries | Every Home/Support/AI/Safety/FAQ tap |
| `PHOTO_FILE_IDS`   | ∞ (Telegram rarely invalidates) | per asset | Every photo screen after first send |
| `STATUS_CACHE`     | 30 s | 4 entries | Every "Live Status" tap |
| `TICKETS_LIST_CACHE` | 10 s | 512 users | Every "My Tickets" tap |
| `AI_REPLY_CACHE`   | 5 min | 512 prompts | Repeated FAQ-shaped questions |
| `CRICKET_CACHE`    | 45 s | 64 endpoints | Every cricket data read |
| `AI_INFLIGHT`      | request-lifetime | unbounded but self-cleaning | Concurrent identical prompts |

Cache invalidation is automatic where it matters:
- Toggling AI in the admin panel → `_settings_cache` purges
- Refreshing Live Status → `STATUS_CACHE` purges
- Any ticket write → that user's `TICKETS_LIST_CACHE` entry purges

---

## Render Free-Tier Notes

- **Single web service**: Uses the existing health-check thread on `$PORT`.
- **Memory footprint**: All caches together stay under ~5 MB on a busy bot.
- **Cold starts**: Static screens are rebuilt at boot in <1 ms; photo `file_id`s
  re-warm naturally on first use after deploy.
- **No new dependencies for caching** — pure stdlib (`OrderedDict`, `time`,
  `hashlib`, `asyncio`).
- **Polling concurrency**: `concurrent_updates(True)` lets multiple users be
  served in parallel without blocking each other on AI calls.

---

## Migration Steps

1. Copy every file from `cricway-optimized/` over the matching file in your
   Render repo (keep your `assets/` folder + `cricway.db` as-is).
2. Bump dependencies: `pip install -r requirements.txt`.
3. Redeploy. The DB schema is unchanged, so no migration runs.
4. (Optional) Set env var `CRICKET_API_BASE` + `CRICKET_API_KEY` if/when you
   wire in `cricket_api.py`.

---

## Expected Impact

| Action | Before | After |
|--------|--------|-------|
| Tap "Home" / "FAQ" / "Safety" | 600–1500 ms (DB + photo upload + delete-send) | ~150–300 ms (cached text + cached file_id + edit) |
| Tap "My Tickets" (warm) | 400–800 ms | ~100–200 ms |
| Send free-form message | 2–6 s (waits on AI) | <300 ms ACK; AI answer arrives 1–4 s later in same message |
| Identical repeat question | full AI call again | **0** AI calls (cache hit) |
| 10 users hit the same FAQ-style question | 10 AI calls | **1** AI call (in-flight dedupe) |
| Cricket API repeated read | every request hits upstream | **1** upstream call per 45 s |
