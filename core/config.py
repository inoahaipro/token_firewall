"""
core/config.py — Single source of truth. Zero hardcoding.
Every setting overridable via environment variable.
"""
import os
import platform
from pathlib import Path


# ── Platform detection ────────────────────────────────────────────────────────

def _detect_platform() -> str:
    if os.path.exists("/data/data/com.termux"):
        return "android"
    if os.path.exists("/private/var/mobile") or os.path.exists("/var/mobile"):
        return "ios"
    s = platform.system().lower()
    return {"darwin": "macos", "windows": "windows"}.get(s, "linux")

PLATFORM = os.environ.get("TF_PLATFORM", _detect_platform())

# ── Data directories ──────────────────────────────────────────────────────────

def _default_data_dir() -> Path:
    return {
        "android": Path("/data/data/com.termux/files/home/.token-firewall"),
        "ios":     Path.home() / "Documents/token-firewall",
        "macos":   Path.home() / "Library/Application Support/token-firewall",
        "linux":   Path.home() / ".local/share/token-firewall",
        "windows": Path(os.environ.get("APPDATA", str(Path.home()))) / "token-firewall",
    }.get(PLATFORM, Path.home() / ".token-firewall")

DATA_DIR = Path(os.environ.get("TF_DATA_DIR", str(_default_data_dir())))
DB_PATH  = Path(os.environ.get("TF_DB_PATH",  str(DATA_DIR / "learned.db")))
PACK_DIR = Path(os.environ.get("TF_PACK_DIR", str(Path(__file__).parents[1] / "packs")))
LOG_PATH = Path(os.environ.get("TF_LOG_PATH", str(DATA_DIR / "analytics.jsonl")))

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Server ────────────────────────────────────────────────────────────────────

HOST = os.environ.get("TF_HOST", "127.0.0.1")
PORT = int(os.environ.get("TF_PORT", 8000))

# ── LLM ──────────────────────────────────────────────────────────────────────

LLM_BASE_URL = os.environ.get("TF_LLM_BASE_URL", "")
LLM_API_KEY  = os.environ.get("TF_LLM_API_KEY",  "")
LLM_MODEL    = os.environ.get("TF_LLM_MODEL",    "")
LLM_TIMEOUT  = int(os.environ.get("TF_LLM_TIMEOUT", 45))

# Semicolon-separated fallbacks: url|key|model;url|key|model
LLM_FALLBACKS = os.environ.get("TF_LLM_FALLBACKS", "")

# ── Cache tuning ──────────────────────────────────────────────────────────────

FUZZY_THRESHOLD     = float(os.environ.get("TF_FUZZY_THRESHOLD",     0.45))
FUZZY_THRESHOLD_APP = float(os.environ.get("TF_FUZZY_THRESHOLD_APP", 0.65))
CACHE_THRESHOLD     = float(os.environ.get("TF_CACHE_THRESHOLD",     0.75))
CACHE_PROMOTE_HITS  = int(os.environ.get("TF_PROMOTE_HITS",          10))
STALE_DAYS          = int(os.environ.get("TF_STALE_DAYS",             30))

# ── Safety ────────────────────────────────────────────────────────────────────

DISABLED_ACTIONS = set(filter(None, os.environ.get("TF_DISABLE_ACTIONS", "").split(",")))
DISABLE_ADB      = os.environ.get("TF_DISABLE_ADB",    "false").lower() == "true"
DISABLE_TERMUX   = os.environ.get("TF_DISABLE_TERMUX", "false").lower() == "true"

# ── LLM System prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = os.environ.get("TF_SYSTEM_PROMPT", """
You are the reasoning backend for Token Firewall, a zero-token AI gateway.

For DEVICE ACTIONS respond with JSON only (no extra text):

Single action:
{"type": "ACTION_TYPE", "description": "what it does", "params": {}}

Multi-step workflow:
{"type": "workflow", "description": "what it does", "steps": [
  {"type": "ACTION_TYPE", "params": {}},
  {"type": "wait", "params": {"seconds": 1}},
  {"type": "ACTION_TYPE", "params": {}}
]}

Available action types:
- open_app       {"package": "com.example", "app_name": "Name"}
- key_event      {"key": "home|back|recent|enter|volume_up|volume_down"}
- tap            {"x": 500, "y": 900}
- swipe          {"x1": 500, "y1": 1400, "x2": 500, "y2": 400, "duration_ms": 300}
- scroll_down    {"steps": 5}
- scroll_up      {"steps": 5}
- type_text      {"text": "..."}
- find_and_tap   {"text": "element label"}
- find_and_type  {"text": "field label", "content": "text to type"}
- adb_command    {"cmd": "shell command"}
- vibrate        {"duration_ms": 500}
- torch          {"state": "on|off"}
- toast          {"message": "..."}
- notify         {"title": "...", "content": "..."}
- take_photo     {"filename": "/sdcard/photo.jpg"}
- battery_status {}
- wifi_info      {}
- wifi_scan      {}
- location       {"provider": "gps|network"}
- clipboard_get  {}
- clipboard_set  {"text": "..."}
- screenshot_adb {"path": "/sdcard/screenshot.png"}
- send_sms       {"number": "1234567890", "message": "text"}
- wait           {"seconds": 1}

Rules:
- Only return JSON for DEVICE ACTIONS.
- Questions, chat, creative writing — plain text only, no JSON.
- When unsure — use plain text.
- Keep responses concise.
""".strip())

# ── Token tracking (in-memory, resets on restart) ─────────────────────────────

_tokens_spent = 0
_tokens_saved = 0

def record_tokens(spent: int = 0, saved: int = 0):
    global _tokens_spent, _tokens_saved
    _tokens_spent += spent
    _tokens_saved += saved

def token_stats() -> dict:
    return {
        "spent": _tokens_spent,
        "saved": _tokens_saved,
        "ratio": f"{round(_tokens_saved / max(_tokens_spent + _tokens_saved, 1) * 100)}% saved",
    }
