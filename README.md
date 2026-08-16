# Token Firewall v3

Zero-token AI gateway. Sits between any OpenAI-compatible client and your LLM.

- **Cache hit** → executes locally, 0 tokens
- **Cache miss** → calls LLM, learns result, next time is 0 tokens
- **Semantic cache** → paraphrased repeats ("check battery" vs "what's my battery at") still hit cache, not just exact text matches
- **Device actions** → executes via ADB + Termux API (Android), rish/Shizuku over SSH (remote Android, no ADB needed), or shell (desktop)
- **Desktop + phone together** → on Linux/macOS/Windows, `CompositeHands` runs desktop shell control and remote-phone rish control side by side, so one TF instance can drive both
- **Built-in chat UI** at `/ui`, no extra client needed to try it out

---

## Quick start (Android/Termux)

```bash
# 1. Install deps
pip install fastapi 'uvicorn[standard]' --break-system-packages

# 2. Install system tools
pkg install android-tools termux-api

# 3. Configure
cp ~/.token-firewall.env ~/.token-firewall.env.bak  # if upgrading from v2
nano ~/.token-firewall.env
# Fill in TF_LLM_BASE_URL, TF_LLM_API_KEY, TF_LLM_MODEL

# 4. Run (with auto-restart)
cd ~/token-firewall-v3 && bash start.sh
```

## Quick start (Desktop + remote phone, rish/Shizuku)

Runs on a PC and controls both itself (shell) and a separate Android phone over
SSH via [Shizuku](https://shizuku.rikka.app/)'s `rish` shell -- no ADB, no USB
connection, no phone unlocked on a desk.

```bash
# 1. Install deps
pip install fastapi 'uvicorn[standard]' --break-system-packages

# 2. Configure. Needs TF_PLATFORM=linux (or macos/windows) so
#    CompositeHands loads both DesktopHands and RishPhoneHands
nano ~/.token-firewall.env
# TF_PLATFORM=linux
# TF_LLM_BASE_URL=...
# TF_LLM_API_KEY=...
# TF_LLM_MODEL=...

# 3. Make sure the phone side is reachable: Shizuku running + rish installed,
#    and a durable SSH key set up to the phone (Termux sshd or similar).
#    See platforms/rish_phone/hands.py for the exact SSH/rish invocation.

# 4. Run (with auto-restart)
cd ~/token-firewall-v3 && python launch.py
```

## Using it from any client

Token Firewall speaks the standard OpenAI API, so anything that lets you
point at a custom base URL works out of the box, no adapter needed:

```
Base URL: http://127.0.0.1:8000/v1
API key:  none (or anything, it's not checked)
Model:    firewall
```

That's it. Point any OpenAI-compatible app, SDK, or CLI at that base URL
and it'll route through the cache automatically.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TF_PLATFORM` | auto-detected | `android`, `ios`, `linux`, `macos`, `windows` |
| `TF_LLM_BASE_URL` | - | LLM API base URL |
| `TF_LLM_API_KEY` | - | LLM API key |
| `TF_LLM_MODEL` | - | Model name |
| `TF_LLM_FALLBACKS` | - | `url\|key\|model;url\|key\|model` |
| `TF_HOST` | `127.0.0.1` | Server bind host |
| `TF_PORT` | `8000` | Server port |
| `TF_FUZZY_THRESHOLD` | `0.45` | Fuzzy match sensitivity |
| `TF_STALE_DAYS` | `30` | Days before cached entries expire |
| `TF_DISABLE_ADB` | `false` | Disable ADB entirely |
| `TF_DISABLE_TERMUX` | `false` | Disable Termux API |
| `TF_DISABLE_ACTIONS` | - | Comma-separated blocked action types |

---

## API endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | OpenAI-compatible completions |
| `/v1/models` | GET | List available models |
| `/v1/capabilities` | GET | What the device can execute |
| `/v1/ui-find` | POST | Find UI element coords via LLM |
| `/v1/export-pack` | POST | Export learned entries to pack |
| `/health` | GET | Status, stats, token savings |
| `/ui` | GET | Built-in chat interface |

---

## Architecture

```
client (any OpenAI-compatible app / built-in UI / curl)
    │
    ▼
server.py  (FastAPI, async, SSE)
    │
    ▼
FirewallRouter
    ├── IntentEngine      classify + split compound intents
    ├── KnowledgeStore    two-tier cache (packs + learned SQLite)
    │     ├── fuzzy lookup      n-gram similarity
    │     └── semantic lookup   embedding cosine-similarity (fastembed/MiniLM)
    ├── Platform Hands    execute device actions
    │     ├── AndroidHands    (Termux API + ADB, running on-device)
    │     ├── RishPhoneHands  (Shizuku/rish over SSH, remote Android control)
    │     ├── DesktopHands    (shell commands)
    │     ├── CompositeHands  (Desktop + RishPhone together, on linux/macos/windows)
    │     └── IOSHands        (future)
    └── LLMAdapter        fallback chain of providers
```

## Adding a new platform

1. Create `platforms/yourplatform/hands.py` implementing `execute(action) → ActionResult`
2. Create `packs/yourplatform/base.json` with platform-specific knowledge entries
3. Add detection in `core/config.py` `_detect_platform()`
4. Add loader in `server.py` `_load_hands()`

## Bugs I hit

Found by actually attacking the thing -- fresh Claude instances using it
blind, plus a live concurrency test that crammed a couple days of realistic
traffic into a few minutes. Code review alone wouldn't have caught most of
this.

**Wrong handler kept winning.** `CompositeHands` checks `DesktopHands` before
`RishPhoneHands`. Fine most of the time, except three different actions
(clipboard, opening an app, screenshot) all had the same failure: Desktop
claimed the action first even when it couldn't actually do it right. Reading
the clipboard silently grabbed the *PC's* clipboard instead of the phone's.
Screenshot was advertised in Desktop's capabilities list with no code behind
it at all, so it just ate the request and returned an error, and the phone's
handler (which works fine) never got a shot. Same bug, three places. Fixed by
not claiming capabilities you don't have, and checking the actual shape of
what's being opened (an Android package id doesn't look like a Windows/Linux
binary name) instead of trusting the action's label.

**Lying instead of saying "I don't know."** Wireless ADB dropped mid-session
and every "open Spotify" started coming back "couldn't find it installed" --
for an app that was very much on the phone. The code had no way to tell
"checked, it's not there" apart from "couldn't check at all." Same exact
mistake was sitting in the health endpoint: `"device" in output` reads as
true even when zero devices are connected, because `adb devices`' own header
text contains the word "device." Both got the same fix -- track whether the
query itself worked, not just what it returned.

**Typo-correction ate real words.** Fuzzy-matching against a fixed keyword
list to fix typed typos also ran on app names, so "spotify" silently became
"notify" (0.77 similarity, above the cutoff), "chrome" became "home," "clock"
became "lock." The command itself would still "succeed" -- just against the
wrong target, no error anywhere. Fixed by leaving the target of an
open/launch command alone; the resolver downstream already does its own
matching on the raw name.

**Shared SQLite connection, zero locking.** `check_same_thread=False` turns
off Python's *safety check* for using one connection across threads. It does
not make the connection thread-safe. Under real concurrent load this crashed
outright with `sqlite3.ProgrammingError`. Took one load test to find, one
lock around every DB call to fix, and it hasn't come back since.

**Every request redid the same slow work.** Resolving an app name meant a
live ~10-17s round trip to list every installed package -- on every single
open-app request, even back-to-back ones for the same app. Worse under
concurrency: N requests hitting a cold cache meant N separate slow round
trips instead of one. Holding the lock across the whole fetch, not just the
read and the write, fixed both at once -- one request pays the cost, the rest
just wait on the lock and find a warm cache.

## Codex automation tips

Use `/v1/ui-find` for intelligent element finding without a vision model:

```bash
curl -X POST http://localhost:8000/v1/ui-find \
  -H "Content-Type: application/json" \
  -d '{"goal": "tap the login button"}'
# → {"x": 540, "y": 1200}
```

Then tap it:
```bash
adb shell input tap 540 1200
```
