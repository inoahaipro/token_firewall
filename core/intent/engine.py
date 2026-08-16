"""
core/intent/engine.py -- Classify and decompose prompts into Intent objects.

Intent types:
  DEVICE  -- maps to a known device action (cache → hands)
  LEARNED -- might be in cache from a previous LLM call
  LLM     -- needs live reasoning
  CHAIN   -- multiple intents joined by "then", "and then", etc.
"""
import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parents[2]))
import core.config as cfg
from core.cache.store import fingerprint


# ── Intent types ──────────────────────────────────────────────────────────────

DEVICE  = "device"
LEARNED = "learned"
LLM     = "llm"
CHAIN   = "chain"


# ── Parameterized templates ───────────────────────────────────────────────────
# These extract a numeric value and produce a stable fingerprint template.
# So "brightness to 75%" and "brightness to 40%" share one cache entry.

_PARAM_PATTERNS = [
    (re.compile(r"(brightness|screen brightness)\s+(?:to\s+)?(\d+)\s*%?", re.I), "brightness"),
    (re.compile(r"(volume)\s+(?:to\s+)?(\d+)\s*%?",                        re.I), "volume"),
    (re.compile(r"(set\s+(?:a\s+)?timer)\s+(?:for\s+)?(\d+)\s*(min|sec|hour)?", re.I), "timer"),
    (re.compile(r"(vibrate)\s+(?:for\s+)?(\d+)\s*(ms|milliseconds?)?",     re.I), "vibrate_ms"),
]

def _extract_param(text: str) -> tuple[str, dict]:
    """Return (template, {param_name: value}). Template has {value} placeholder."""
    for pattern, name in _PARAM_PATTERNS:
        m = pattern.search(text)
        if m:
            value = m.group(2)
            template = pattern.sub(lambda x: x.group(0).replace(value, "{value}"), text)
            return template.lower().strip(), {name: value}
    return text.lower().strip(), {}


# ── Keyword heuristics ────────────────────────────────────────────────────────

_DEVICE_KW = {
    "mobile": [
        # power / battery
        "battery", "charging", "power",
        # connectivity
        "wifi", "wi-fi", "wireless", "bluetooth", "mobile data", "airplane",
        "hotspot", "network",
        # communication
        "sms", "text message", "call", "phone",
        # media / camera
        "camera", "photo", "picture", "screenshot", "record", "video",
        # controls
        "flashlight", "torch", "volume", "brightness", "dim", "loud", "quiet",
        "mute", "silent", "ring", "notification", "toast", "notify", "alert",
        # sensors / location
        "vibrate", "shake", "gps", "location", "coordinates",
        # navigation / UI
        "tap", "click", "press", "swipe", "scroll", "open", "launch",
        "go home", "go back", "recent apps", "type text", "type",
        "lock", "unlock", "screen", "display",
        # system
        "clipboard", "settings", "do not disturb", "dnd",
        "restart", "reboot", "shutdown",
    ],
    "desktop": [
        "open app", "close window", "screenshot", "clipboard",
        "file", "folder", "terminal", "process", "kill", "launch",
    ],
    "shared": [
        "set timer", "set alarm", "start timer", "stop timer", "cancel alarm",
    ],
}

_PLATFORM_GROUP = {
    "android": "mobile",
    "ios":     "mobile",
    "linux":   "desktop",
    "macos":   "desktop",
    "windows": "desktop",
}

_LLM_KW = [
    "write", "explain", "summarize", "analyse", "analyze", "code",
    "debug", "fix", "refactor", "generate", "design", "translate",
    "compare", "review", "suggest", "why", "how does", "what is",
    "what are", "what was", "what time", "what date", "what's the",
    "whats the", "help me", "tell me", "can you", "who is",
    "where is", "when did", "how many", "how much", "what should",
    "hi", "hello", "hey", "thanks", "thank you", "please",
    "give me", "show me", "find me", "list", "describe",
]

_CHAIN_SPLITTER = re.compile(
    r"\b(then|and then|after that|followed by|next|finally|also)\b",
    re.IGNORECASE,
)

# Simple no-parameter status checks: ANY phrasing containing these keywords
# (and no "set/turn/change" verb implying a different action) maps to the SAME
# canonical fingerprint. Without this, "check battery" and "what's my battery"
# are both correctly classified as DEVICE intents but get different cache
# entries since the fingerprint was built from the raw phrase -- so repeat
# checks phrased differently never actually hit cache. This fixes that.
_CANONICAL_STATUS_CHECKS = {
    "battery_status": ["battery", "charged", "charging", "charge", "power level", "juice"],
    "wifi_info":       ["wifi", "wi-fi", "wireless network", "wireless info"],
    "clipboard_get":   ["clipboard"],
    "location":        ["gps", "location", "coordinates"],
}
_ACTION_VERB_RE = re.compile(
    r"\b(set|turn on|turn off|enable|disable|change|toggle|connect|disconnect)\b",
    re.IGNORECASE,
)

# Typo tolerance: local, zero-token, no LLM call. Corrects garbled words
# against the vocabulary TF actually cares about (device keywords + common
# command verbs) BEFORE keyword/canonical matching runs, so e.g. "chek my
# batery" still classifies and caches the same as "check my battery"
# instead of falling through to a real LLM call every time.
_TYPO_VOCAB = set()
for _lst in _DEVICE_KW.values():
    for _kw in _lst:
        _TYPO_VOCAB.update(_kw.split())
for _kw in _LLM_KW:
    _TYPO_VOCAB.update(_kw.split())
_TYPO_VOCAB.update({"check", "get", "show", "tell", "whats", "status", "please", "send"})
_TYPO_VOCAB = {w for w in _TYPO_VOCAB if len(w) >= 4}  # short words are too ambiguous to safely correct


# "open/launch/start/run <name>" -- the <name> is an app name, not a device
# keyword, and app names routinely collide with the vocab by pure string
# similarity (spotify~notify, chrome~home, clock~lock, maps~apps,
# whatsapp~whats). _try_open_unknown's own app resolver already does its own
# fuzzy substring search on the raw name, so typo-correcting the name here
# only pre-corrupts it before that resolver ever sees it. Matches router.py's
# own _try_open_unknown verb set exactly.
_OPEN_TARGET_RE = re.compile(r"^(open|launch|start|run)\s+(.+)$", re.I)


def _typo_correct(norm: str) -> str:
    m = _OPEN_TARGET_RE.match(norm)
    if m:
        # target name is left untouched -- resolver does its own fuzzy match
        return f"{m.group(1)} {m.group(2)}"
    words = norm.split()
    out = []
    for w in words:
        core = w.strip(".,!?'\"")
        if len(core) < 4 or core in _TYPO_VOCAB:
            out.append(w)
            continue
        match = difflib.get_close_matches(core, _TYPO_VOCAB, n=1, cutoff=0.75)
        out.append(match[0] if match else w)
    return " ".join(out)


def _canonical_status_fp(norm: str, platform: str) -> Optional[str]:
    if _ACTION_VERB_RE.search(norm):
        return None  # "turn off wifi" etc is a different action, don't canonicalize
    for canonical, keywords in _CANONICAL_STATUS_CHECKS.items():
        if any(kw in norm for kw in keywords):
            return fingerprint(f"__status__{canonical}", platform)
    return None


# Content-bearing requests (toast/notify/clipboard/type) can't safely
# fuzzy-match on the whole phrase (that's what let "saying hi" replay a
# completely different cached message once) -- but keying the EXACT cache
# on the extracted payload itself, ignoring incidental surrounding wording,
# is both safe AND useful: "send a toast saying hi" and "send another toast
# saying hi" carry the identical actual content, so there's no reason the
# second one should cost real tokens just because the framing words around
# "hi" differ. Different content still gets a genuinely different
# fingerprint -- this only helps when the payload itself repeats.
_CONTENT_MESSAGE_RE = re.compile(
    r"(?:toast|notif\w*|clipboard|type)\w*\b.*?"
    r"(?:saying|says|say|reading|with the (?:word|text|message)|to say|that says|what says)\s+"
    r"(.+?)\.?$",
    re.IGNORECASE,
)


def _canonical_content_fp(norm: str, platform: str) -> Optional[str]:
    m = _CONTENT_MESSAGE_RE.search(norm)
    if not m:
        return None
    content = m.group(1).strip()
    if not content:
        return None
    return fingerprint(f"__content__{content}", platform)


# Queries that must never be cached (time-sensitive / creative / conversational)
_NO_CACHE = re.compile(
    r"\b(today|right now|currently|current time|what time|what day|"
    r"this week|yesterday|tomorrow|weather|news|latest|breaking|"
    r"tell me a joke|jokes?|random|poem|song|story|roleplay|brainstorm)\b",
    re.IGNORECASE,
)


# ── Intent dataclass ──────────────────────────────────────────────────────────

@dataclass
class Intent:
    raw:         str
    normalized:  str
    kind:        str            # DEVICE | LEARNED | LLM | CHAIN
    fingerprint: str
    platform:    str
    params:      dict = field(default_factory=dict)
    cacheable:   bool = True
    sub_intents: list["Intent"] = field(default_factory=list)


# ── Engine ────────────────────────────────────────────────────────────────────

class IntentEngine:

    def __init__(self, platform: Optional[str] = None):
        self.platform = platform or cfg.PLATFORM

    def process(self, text: str) -> Intent:
        text = self._clean(text)
        parts = self._split_chain(text)

        if len(parts) > 1:
            subs = [self._classify(p.strip()) for p in parts if p.strip()]
            return Intent(
                raw=text, normalized=text.lower().strip(),
                kind=CHAIN, fingerprint=fingerprint(text.lower(), self.platform),
                platform=self.platform, sub_intents=subs,
            )

        return self._classify(text.strip())

    def _clean(self, text: str) -> str:
        """Strip OpenClaw metadata blocks and timestamp prefixes."""
        if "```" in text:
            text = text.split("```")[-1]
        text = re.sub(r"^\[[^\]]+\]\s*", "", text.strip())
        return text.strip()

    def _split_chain(self, text: str) -> list[str]:
        parts = _CHAIN_SPLITTER.split(text)
        connectors = {c.lower() for c in _CHAIN_SPLITTER.findall(text)}
        return [p for p in parts if p.strip().lower() not in connectors and p.strip()]

    def _classify(self, text: str) -> Intent:
        norm = _typo_correct(text.lower().strip())
        template, params = _extract_param(norm)
        fp = fingerprint(template, self.platform)
        cacheable = not bool(_NO_CACHE.search(norm))

        # This deployment runs desktop (root shell) AND phone (rish/SSH) hands
        # simultaneously, not just cfg.PLATFORM's own group -- check both
        # keyword lists, not just the one matching the machine TF happens to
        # be running on. Otherwise phone-only keywords like "battery"/"wifi"
        # never classify as DEVICE at all when TF runs on the PC.
        device_keys = _DEVICE_KW.get("mobile", []) + _DEVICE_KW.get("desktop", []) + _DEVICE_KW.get("shared", [])

        if any(kw in norm for kw in device_keys):
            canonical_fp = _canonical_content_fp(norm, self.platform) or _canonical_status_fp(norm, self.platform)
            return Intent(raw=text, normalized=norm, kind=DEVICE,
                          fingerprint=canonical_fp or fp, platform=self.platform,
                          params=params, cacheable=cacheable)

        if any(kw in norm for kw in _LLM_KW):
            return Intent(raw=text, normalized=norm, kind=LLM,
                          fingerprint=fp, platform=self.platform,
                          params=params, cacheable=cacheable)

        return Intent(raw=text, normalized=norm, kind=LEARNED,
                      fingerprint=fp, platform=self.platform,
                      params=params, cacheable=cacheable)
