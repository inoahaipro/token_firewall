# Token Firewall v3

Zero-token AI gateway. Sits between OpenClaw (or any OpenAI-compatible client) and your LLM.

- **Cache hit** → executes locally, 0 tokens
- **Cache miss** → calls LLM, learns result, next time is 0 tokens
- **Device actions** → executes via ADB + Termux API (Android), shell (desktop)
- **Built-in chat UI** at `/ui`, no OpenClaw required

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

## OpenClaw config

Add to `~/.openclaw/openclaw.json` under `models.providers`:

```json
"token-firewall": {
  "baseUrl": "http://127.0.0.1:8000/v1",
  "apiKey": "none",
  "api": "openai-completions",
  "models": [{"id": "firewall", "name": "Token Firewall", "input": ["text"]}]
}
```

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
client (OpenClaw / built-in UI / curl)
    │
    ▼
server.py  (FastAPI, async, SSE)
    │
    ▼
FirewallRouter
    ├── IntentEngine      classify + split compound intents
    ├── KnowledgeStore    two-tier cache (packs + learned SQLite)
    ├── Platform Hands    execute device actions
    │     ├── AndroidHands   (Termux API + ADB)
    │     ├── DesktopHands   (shell commands)
    │     └── IOSHands       (future)
    └── LLMAdapter        fallback chain of providers
```

## Adding a new platform

1. Create `platforms/yourplatform/hands.py` implementing `execute(action) → ActionResult`
2. Create `packs/yourplatform/base.json` with platform-specific knowledge entries
3. Add detection in `core/config.py` `_detect_platform()`
4. Add loader in `server.py` `_load_hands()`

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
