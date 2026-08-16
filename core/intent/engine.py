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

# A device keyword appearing ANYWHERE in the text used to be sufficient for
# kind=DEVICE, no matter how it's used grammatically. "screen" is a real
# device keyword; "my screen is cracked" and "battery died" are statements
# about a device, not commands to one, and matched anyway. Real commands
# are imperative: check/turn/send/open/take, no subject, present tense.
# Narrated statements have a subject and a descriptive/past-tense verb the
# device vocabulary doesn't cover ("died", "cracked", "broke", "froze",
# "left", "sucks"). Requiring one of these action verbs is the actual
# distinguishing signal -- not message length (that was a symptom-level
# patch for one manifestation of this, kept as defense in depth below, not
# a substitute for this).
_ACTION_VERBS = {
    "check", "get", "show", "tell", "whats", "what's",
    "turn", "enable", "disable", "toggle", "set", "change", "adjust",
    "send", "notify", "alert", "toast",
    "open", "launch", "start", "close", "stop", "kill", "quit", "exit",
    "take", "grab", "tap", "click", "press", "swipe", "scroll", "type",
    "run", "execute", "vibrate", "connect", "disconnect",
    "lock", "unlock", "mute", "unmute", "dim", "brighten",
    "raise", "lower", "increase", "decrease",
    "restart", "reboot", "shutdown", "find", "search", "go",
}

# Bare topic/status queries have no verb at all ("battery", "wifi status",
# "battery?") and are legitimate short-circuits around the action-verb
# requirement above -- but ONLY if literally every word in the message is
# either device vocabulary or one of these query/filler words. One
# unrecognized word (a real verb, an adjective, a place, anything outside
# this small closed set) means there's actual sentence content the fast
# path can't safely interpret, and it should fall through to a real LLM
# call instead of guessing. "battery died": "died" isn't in this set, so
# it correctly requires an action verb (has none) and isn't classified as
# a command.
_SAFE_QUERY_FILLER = {
    "my", "is", "on", "off", "the", "a", "an", "please", "pls", "plz",
    "whats", "what's", "am", "i", "do", "have", "many", "connected",
    "status", "level", "percentage", "charge", "charging", "info",
    "at", "of", "to", "in", "for", "how", "much", "current", "right", "now",
    "it", "this", "that",
}
_WORD_RE = re.compile(r"[a-z']+")

# Leading politeness/filler stripped before looking for the verb -- "please
# check my battery" and "can you turn on wifi" both put the real verb after
# a couple of throwaway words, not literally at position 0.
_LEADING_FILLER = {"please", "hey", "yo", "so", "can", "could", "would", "you", "just", "now"}


def _looks_like_command(raw_text: str, norm: str) -> bool:
    # Typo-tolerant action-verb match, but ONLY against the leading word(s)
    # of the RAW (pre-typo-correction) message, not a scan of the whole
    # thing. Found live: "wifi at the coffee shop sucks" -- pure narrative,
    # zero command intent -- got "shop" typo-corrected to "stop" (0.75
    # similarity, exact same collision class as spotify~notify) and "stop"
    # is a real action verb, manufacturing a false command signal. Turns
    # out there's no similarity cutoff that separates "sned"~"send" from
    # "shop"~"stop" -- both score exactly 0.75, identical. The signal that
    # actually works is position: real commands are imperative, verb first
    # ("check battery", "turn on wifi"); "shop" sits mid-sentence in a
    # narrative, "sned"/"chek"/"tunr" sit at the very front of a command.
    # Restricting the fuzzy match to leading words only keeps real typo
    # tolerance ("chek my batery" still works) while a decoy word buried
    # later in a sentence never gets the chance to match at all.
    raw_words = _WORD_RE.findall(raw_text.lower())
    lead = [w for w in raw_words if w not in _LEADING_FILLER][:2]
    for w in lead:
        if w in _ACTION_VERBS:
            return True
        if len(w) >= 4 and difflib.get_close_matches(w, _ACTION_VERBS, n=1, cutoff=0.75):
            return True
    # Bare-query fallback (no verb needed, e.g. "battery", "wifi status")
    # DOES use the corrected `norm` -- typo tolerance on the device NOUN
    # itself ("wifii status" -> "wifi status") is legitimate and safe here,
    # since this branch already requires every remaining word to be
    # recognized vocabulary, not just a loose similarity match.
    words = set(_WORD_RE.findall(norm))
    device_vocab_words = set()
    for _lst in _DEVICE_KW.values():
        for _kw in _lst:
            device_vocab_words.update(_kw.split())
    allowed = device_vocab_words | _SAFE_QUERY_FILLER
    return not (words - allowed)

_CHAIN_SPLITTER = re.compile(
    r"\b(then|and then|after that|followed by|next|finally|also)\b",
    re.IGNORECASE,
)

# Real chain commands are short and dense with imperative verbs -- "turn on
# wifi then send a toast saying dinner's ready" is 10 words for a genuine
# 2-step command. Narrated/conversational sentences run longer with mostly
# non-command filler. See process()'s use of this for the full rationale.
# 10, not 11 -- an 11-word narrative sentence ("Screen went black and froze
# for a minute. Then came back.") still reproduced the bug at the original
# threshold; verified 10 is the highest value that both blocks that case
# and still lets the 10-word legit-chain example through.
_LONG_MESSAGE_WORDS = 10

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
    # Guard directly here, not just at the classification level above --
    # this function's output also becomes Intent.normalized, which callers
    # downstream (cache lookups, the actual upstream LLM prompt on a miss)
    # can use verbatim. Skipping device classification for long messages
    # doesn't help if the mangled text still leaks through as what gets
    # sent to the LLM or cached under.
    if len(norm.split()) > _LONG_MESSAGE_WORDS:
        return norm
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

        # Long messages skip chain-splitting AND device-keyword classification
        # entirely, straight to LLM handling. Found live: "my screen went dark
        # and froze for a minute then came back" -- a plain narrated sentence,
        # not a command -- got split on "then" into two fragments ("...for a
        # minute" / "came back"), each short enough that typo-correction (see
        # _typo_correct) mangled them into "mute"/"camera", and BOTH still
        # would have classified as real device intents even without that,
        # since _classify's device check is a bare substring match against
        # ~60 keywords with zero structural awareness -- "screen" alone is
        # one of them. Real chain commands ("turn on wifi then send a toast")
        # are short and dense with imperative verbs; narrated sentences run
        # longer and are mostly non-command filler. A real LLM call correctly
        # tells these apart; the free local classifier can't, so past this
        # length it shouldn't guess.
        if len(text.split()) > _LONG_MESSAGE_WORDS:
            return self._classify(text.strip(), skip_device_kw=True)

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

    def _classify(self, text: str, skip_device_kw: bool = False) -> Intent:
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

        if not skip_device_kw and any(kw in norm for kw in device_keys) and _looks_like_command(text, norm):
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
