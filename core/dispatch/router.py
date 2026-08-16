"""
core/dispatch/router.py -- FirewallRouter v3

Flow per request:
  1. IntentEngine classifies prompt → Intent
  2. CHAIN → route each sub-intent, join results
  3. DEVICE → exact cache → fuzzy cache → unknown app → LLM planning
  4. LEARNED/LLM → no-cache check → exact cache → fuzzy cache → LLM → cache write

v3 improvements over v2:
  - Smart fuzzy bypass (gesture/UI intents skip open_app cache hits)
  - App+type-verb detection ("open WhatsApp and message X" → full workflow)
  - Workflow list execution (action can be list, workflow dict, or single dict)
  - UI dump reasoning via LLM (ui_find method)
  - Analytics JSONL logging
  - Brightness % → Android 0-255 in adb_command
  - adb_command bare OK → description fallback
"""
import json
import re
import threading
import time
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parents[2]))
import core.config as cfg
from core.cache.store import KnowledgeStore, CacheEntry, fingerprint
from core.intent.engine import IntentEngine, Intent, DEVICE, LEARNED, LLM, CHAIN, _CANONICAL_STATUS_CHECKS


def _category_mismatch(query_norm: str, action_type: str) -> bool:
    """True if the query text mentions a DIFFERENT known status-check category
    than the cached entry's own action type -- e.g. query says 'wifi' but the
    matched cache entry is battery_status. Catches cross-topic false-positive
    matches from fuzzy/semantic lookup (found live: 'check wifi' matched a
    cached battery_status entry via generic word overlap -- silently wrong
    data is worse than a cache miss, so any such mismatch is rejected)."""
    for category, keywords in _CANONICAL_STATUS_CHECKS.items():
        if category == action_type:
            continue
        if any(kw in query_norm for kw in keywords):
            return True
    return False


@dataclass
class Response:
    content:      str
    tokens_spent: int = 0
    source:       str = "unknown"


# ── No-cache patterns ─────────────────────────────────────────────────────────

_NO_CACHE_TIME = re.compile(
    r"\b(today|right now|currently|current time|what time|what day|"
    r"this week|yesterday|tomorrow|weather|news|latest|breaking)\b", re.I)
_NO_CACHE_CREATIVE = re.compile(
    r"\b(tell me a joke|jokes?|poem|song lyrics|roleplay|brainstorm|"
    r"short story|novel|screenplay|random)\b", re.I)
_NO_CACHE_CONVO = re.compile(
    r"^(hi|hey|hello|good morning|good afternoon|good evening|"
    r"what'?s up|sup|how are you|how r u|"
    r"what (model|llm|ai) are? you|who are you|what are you|what can you do)\b", re.I)

_APP_TYPING_APPS = {
    "chatgpt","claude","notes","whatsapp","telegram","instagram",
    "messenger","twitter","reddit","discord","gmail","messages",
}
_GARBAGE = [
    "bash arg:","events injected:","java.lang.","activitynotfoundexception",
    "force finishing activity","does not exist","no activities found",
    "error type","exception occurred",
    # Real execution failures -- caching these as if they were successful
    # results meant a genuine bug (e.g. wrong action type for this
    # platform) got permanently replayed from cache forever, even after
    # the underlying code was fixed, since the stale cache entry never
    # re-consulted the LLM.
    "no hands claim action", "action failed.", "action failed",
    "refused:", "is disabled.",
]
_BARE_OK = {"ok","done",""}


def _is_garbage(text: str) -> bool:
    if not text: return False
    low = text.lower().strip()
    if low in _BARE_OK: return True
    return any(p in low for p in _GARBAGE)

def _substitute(action: dict, params: dict) -> dict:
    if not params: return action
    s = json.dumps(action)
    for k, v in params.items():
        s = s.replace("{value}", str(v))
        s = s.replace(f"{{{k}}}", str(v))
    try: return json.loads(s)
    except Exception: return action

def _fix_brightness(cmd: str) -> str:
    def _c(m):
        v = int(m.group(1))
        return str(round(v * 255 / 100)) if v <= 100 else str(v)
    return re.sub(r"\bscreen_brightness (\d+)", lambda m: f"screen_brightness {_c(m)}", cmd)

def _extract_action(text: str) -> Optional[dict]:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(1))
            if "type" in d: return d
        except json.JSONDecodeError: pass
    for marker in ('{"type"', '{ "type"'):
        start = text.find(marker)
        if start == -1: continue
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        d = json.loads(text[start:i+1])
                        if "type" in d and d["type"] != "llm_response": return d
                    except json.JSONDecodeError: pass
                    break
    return None

# Real code-level guardrail against destructive shell commands. Nothing in
# the hands-execution path checked for this at all before -- safety
# depended entirely on the calling LLM choosing not to produce a destructive
# action, with zero backstop if it did. This runs regardless of what
# produced the command (LLM, cache replay, or a future caller), so a bad
# cache entry or a jailbreak-y prompt can't silently execute something
# destructive just because it got past the model once.
#
# First version was a single narrow regex and, on adversarial re-testing,
# missed almost everything that wasn't the EXACT literal syntax it was
# written for -- long-form flags (rm --recursive --force), RCE-via-pipe
# (curl ... | bash), non-rm deletion (find -delete, shutil.rmtree),
# security-disabling commands, chmod/chown wipeouts, arbitrary file
# overwrite, git force-ops, and a generalized fork bomb. Rewritten to check
# flag SEMANTICS (does this rm have recursive+force intent, regardless of
# how the flags are spelled) rather than exact syntax, plus broader
# category coverage. Still not exhaustive -- this is defense in depth on
# top of the calling LLM's own judgment, not a substitute for it.
_RM_INVOCATION_RE = re.compile(r"\brm\s+([^\n;|&]*)", re.I)


def _rm_is_recursive_and_forced(args: str) -> bool:
    recursive = force = False
    for tok in args.split():
        low = tok.lower()
        if low in ("-r", "-R", "--recursive"):
            recursive = True
        elif low == "--force":
            force = True
        elif tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            letters = tok[1:].lower()
            if "r" in letters:
                recursive = True
            if "f" in letters:
                force = True
    return recursive and force


_DESTRUCTIVE_CMD_RE = re.compile(
    r"\bmkfs\b|\bmkfs\.\w+\b"                    # format a filesystem
    r"|\bdd\s+.*\bof=/dev/"                      # dd onto a raw device
    r"|>\s*/dev/sd\w*\b"                         # overwrite a block device
    r"|>\s*/etc/\S+|>\s*/boot/\S+"               # overwrite critical config/boot files
    r"|\btruncate\s+-s\s*0\s+/(?:etc|boot)/"     # zero out critical files
    r"|([\w:]+)\s*\(\)\s*\{\s*\1\s*\|\s*\1\s*&?\s*\}\s*;\s*\1"  # fork bomb, any function name (incl. classic ':')
    r"|\bfactory\s*reset\b|wipe_data|erase_all_data"
    r"|\bdrop\s+(table|database)\b"
    r"|\bshred\b|\bwipefs\b"
    r"|\b(curl|wget)\b[^\n;]*\|\s*(sudo\s+)?(bash|sh|zsh|python3?|perl|ruby)\b"  # RCE via pipe-to-shell
    r"|\b(curl|wget)\b[^\n;]*\|\s*base64\b"      # RCE via pipe-to-base64-decode
    r"|\bbase64\s+(-d|--decode)\b[^\n;]*\|\s*(sudo\s+)?(bash|sh|zsh|python3?|perl|ruby)\b"  # base64-smuggled payload piped to a shell
    r"|\bfind\b[^\n;]*-delete\b"                 # find ... -delete
    r"|\bfind\b[^\n;]*\|\s*xargs\s+rm\b"         # find | xargs rm
    r"|\bfind\b[^\n;]*-exec\s+rm\b"              # find ... -exec rm {} \; / +
    r"|shutil\.rmtree\("                         # python one-liner delete
    r"|\bunlink\s*\(?\s*glob\s*\("                # perl bulk-delete via glob (paren after unlink optional)
    r"|\bsetenforce\s+0\b|\biptables\s+-F\b|\bufw\s+disable\b"
    r"|\bsystemctl\s+(stop|disable)\s+firewalld\b"
    r"|enforcing.{0,10}disabled"                 # sed-editing selinux config, etc
    r"|\bchmod\s+(-R\s+)?000\s+/(?:\s|home|etc|usr|var|$)"
    r"|\bchown\s+-R\s+\S+\s+/(?:\s|$)"
    r"|\bgit\s+push\s+[^\n;]*(-f\b|--force\b)"
    r"|\bgit\s+reset\s+--hard\b"
    r"|\bgit\s+clean\s+-[a-zA-Z]*f[a-zA-Z]*\b",
    re.I,
)

# Python's own recursive-walk-and-delete pattern (os.walk + os.remove/rmdir/
# unlink in the same one-liner) has no "rm"/"shutil.rmtree(" substring at
# all, so it needs its own check rather than fitting the regex above.
_PY_WALK_RE = re.compile(r"os\.walk\(", re.I)
_PY_REMOVE_RE = re.compile(r"os\.(remove|unlink|rmdir)\(", re.I)


_ACTION_META_KEYS = {"type", "description", "params", "steps"}


def _normalize_action(action):
    """LLM output for an action is inconsistent about whether payload
    fields (message, text, cmd, title, content, etc) live nested under
    "params" (the documented schema) or flat at the root. A flat
    {"type":"toast","message":"X"} fired a REAL blank toast, because every
    hands.execute() only ever reads action["params"]["message"] -- the
    root-level value was silently ignored, not an error, just empty content
    sent to the device. Worse: the destructive-command guard reads from the
    same params path, so a flat {"type":"run_command","cmd":"rm -rf /"}
    would look like an empty, harmless command to it -- a genuine guardrail
    bypass, not a hypothetical one, same root cause. Normalize once,
    centrally, so every consumer sees one consistent shape regardless of
    which way the LLM happened to format it that time."""
    if not isinstance(action, dict):
        return action
    if action.get("type") == "workflow":
        return {**action, "steps": [_normalize_action(s) for s in action.get("steps", [])]}
    loose = {k: v for k, v in action.items() if k not in _ACTION_META_KEYS}
    if not loose:
        return action
    params = dict(action.get("params") or {})
    for k, v in loose.items():
        params.setdefault(k, v)
    return {**action, "params": params}


def _is_destructive_command(cmd: str) -> bool:
    cmd = cmd or ""
    # $IFS / ${IFS} is bash's word-splitting variable, commonly substituted
    # for a literal space specifically to dodge \s+-based regexes -- found
    # live via adversarial testing ("rm${IFS}-rf${IFS}/tmp/x" evaded the
    # original check). Normalize it to a real space before matching so the
    # rest of the logic doesn't need to special-case it everywhere.
    cmd = re.sub(r"\$\{?IFS\}?", " ", cmd)
    for m in _RM_INVOCATION_RE.finditer(cmd):
        if _rm_is_recursive_and_forced(m.group(1)):
            return True
    if _PY_WALK_RE.search(cmd) and _PY_REMOVE_RE.search(cmd):
        return True
    return bool(_DESTRUCTIVE_CMD_RE.search(cmd))


# Query/get-type actions return data, not a fire-and-forget confirmation.
# Falling back to a canned "Done." when these return empty output is
# actively misleading (found live: "what's on my clipboard" -> "Done."
# read as "I copied something" when nothing was actually retrieved).
# Fire-and-forget actions (toast, tap, vibrate, etc) legitimately have
# no output to show, so a confirmation string is correct for those --
# just not for anything that's supposed to answer a question.
_QUERY_TYPES = {
    "clipboard_get", "battery_status", "wifi_info", "wifi_scan",
    "location", "get_screen_state",
}


def _confirm(atype: str) -> str:
    return {
        "vibrate":"Done -- vibrated.","torch":"Done -- flashlight toggled.",
        "take_photo":"Done -- photo taken.","send_sms":"Done -- SMS sent.",
        "open_app":"Done -- app opened.","close_app":"Done -- app closed.",
        "key_event":"Done.","tap":"Done -- tapped.","long_press":"Done -- long pressed.",
        "swipe":"Done -- swiped.","scroll_up":"Done -- scrolled up.",
        "scroll_down":"Done -- scrolled down.","type_text":"Done -- typed.",
        "screenshot_adb":"Done -- screenshot saved.","clipboard_set":"Done -- copied.",
        "find_and_tap":"Done -- tapped element.","find_and_type":"Done -- typed into field.",
        "find_and_scroll":"Done -- scrolled.","adb_command":"Done.","wait":"Done -- waited.",
        "run_command":"Done.",
    }.get(atype, "Done.")

def _format(atype: str, raw: str) -> str:
    try: d = json.loads(raw)
    except (json.JSONDecodeError, TypeError): return raw.strip()
    if atype == "battery_status":
        pct = d.get("percentage","?")
        status = d.get("status","unknown").lower()
        health = d.get("health","?").title()
        temp = d.get("temperature","?")
        plugged = d.get("plugged","").replace("PLUGGED_","").title()
        plug = f" ({plugged})" if plugged else ""
        tail = f"Health: {health}, Temp: {temp}°C"
        if status == "full": return f"Battery {pct}% -- fully charged{plug}. {tail}"
        elif status == "charging": return f"Battery {pct}% -- charging{plug}. {tail}"
        else: return f"Battery {pct}% -- discharging. {tail}"
    if atype == "wifi_info":
        return f"WiFi: {d.get('ssid','?')} · IP: {d.get('ip','?')} · {d.get('link_speed_mbps','?')} Mbps"
    if atype == "location":
        return f"Location: {d.get('latitude','?')}, {d.get('longitude','?')} (±{d.get('accuracy','?')}m)"
    if atype == "wifi_scan" and isinstance(d, list):
        return "\n".join(f"{n.get('ssid','hidden')} ({n.get('level','?')} dBm)" for n in d[:8])
    if isinstance(d, dict):
        return "\n".join(f"{k}: {v}" for k, v in d.items())
    return str(raw)


class FirewallRouter:

    def __init__(self, store: KnowledgeStore, engine: IntentEngine, hands, llm):
        self.store  = store
        self.engine = engine
        self.hands  = hands
        self.llm    = llm
        self._requests     = 0
        self._cache_hits   = 0
        self._tokens_spent = 0
        self._tokens_saved = 0
        # Requests now genuinely run concurrently (offloaded to worker
        # threads via asyncio.to_thread in server.py), so these counters --
        # previously bare += on shared instance attributes -- need a lock
        # to avoid lost updates under real concurrent access. Low severity
        # (stats-only, not correctness-critical), fixed anyway while adding
        # real concurrency made it a live possibility instead of dead code.
        self._stats_lock = threading.Lock()

    def _bump(self, requests=0, cache_hits=0, tokens_spent=0, tokens_saved=0):
        with self._stats_lock:
            self._requests     += requests
            self._cache_hits   += cache_hits
            self._tokens_spent += tokens_spent
            self._tokens_saved += tokens_saved

    def route(self, prompt: str, history: list = None) -> Response:
        self._bump(requests=1)
        # history is threaded through as an explicit parameter, not stored as
        # self._history. This router is a single shared instance across all
        # requests, and self._history as a plain attribute set at the top of
        # route() and read again later in the same call is exactly the kind
        # of thing that looks fine until two requests genuinely interleave
        # (multi-worker deployment, or converting these routes to sync def
        # so Starlette's threadpool kicks in) -- request B's history would
        # silently stomp request A's mid-flight, sending the wrong
        # conversation history to the LLM. Only masked right now because
        # this server happens to fully serialize requests today.
        history = history or []
        intent = self.engine.process(prompt)
        if intent.kind == CHAIN and intent.sub_intents:
            resp = self._route_chain(intent, history)
        else:
            resp = self._route_one(intent, history)
        self._log(intent, resp)
        return resp

    def _route_chain(self, chain: Intent, history: list) -> Response:
        parts, total = [], 0
        for sub in chain.sub_intents:
            r = self._route_one(sub, history)
            total += r.tokens_spent
            parts.append(r.content if r.source not in ("hands_error","llm_error") else f"[{sub.normalized}: failed]")
            time.sleep(0.3)
        return Response(" → ".join(parts), tokens_spent=total, source="chain")

    def _route_one(self, intent: Intent, history: list) -> Response:
        print(f"[ROUTE] {intent.kind} {intent.normalized!r}")

        if intent.kind == DEVICE:
            entry = self.store.lookup(intent.fingerprint)
            if entry:
                r = self._exec_cached(entry, intent.fingerprint, intent.params)
                if r is not None:
                    self._bump(cache_hits=1, tokens_saved=500)
                    return r

            entry = self.store.fuzzy_lookup(intent.normalized)
            if entry:
                atype = entry.action.get("type")
                norm  = intent.normalized
                # Skip adb_command cache for gesture intents
                if atype == "adb_command" and any(kw in norm for kw in ("tap","click","press","swipe","scroll")):
                    entry = None
                # Skip open_app for "open X and type/message Y"
                if entry and atype == "open_app" and "open" in norm:
                    if any(v in norm for v in ("type","say","message","enter","write","send")) and \
                       any(app in norm for app in _APP_TYPING_APPS):
                        entry = None
                # Reject cross-topic false matches (e.g. "check wifi" fuzzy-matching a cached battery_status entry)
                if entry and _category_mismatch(norm, atype):
                    print(f"[FUZZY] rejected cross-topic match: '{norm}' vs {atype}")
                    entry = None
            if entry:
                print(f"[FUZZY] '{intent.normalized}' → {entry.action.get('type')}")
                r = self._exec_cached(entry, None, intent.params)
                if r is not None:
                    self._bump(cache_hits=1, tokens_saved=500)
                    return r

            entry = self.store.semantic_lookup(intent.normalized)
            if entry and _category_mismatch(intent.normalized, entry.action.get("type")):
                print(f"[SEMANTIC] rejected cross-topic match: '{intent.normalized}' vs {entry.action.get('type')}")
                entry = None
            if entry:
                r = self._exec_cached(entry, None, intent.params)
                if r is not None:
                    self._bump(cache_hits=1, tokens_saved=500)
                    return r

            if re.match(r"^(?:open|launch|start|run)\s+.+", intent.normalized, re.I):
                r = self._try_open_unknown(intent.normalized)
                if r is not None: return r

            return self._call_llm(intent, history)

        # LEARNED / LLM
        if (not intent.cacheable
                or _NO_CACHE_TIME.search(intent.normalized)
                or _NO_CACHE_CREATIVE.search(intent.normalized)
                or _NO_CACHE_CONVO.search(intent.normalized)):
            return self._llm_passthrough(intent, history)

        entry = self.store.lookup(intent.fingerprint)
        if entry:
            r = self._serve_cached_llm(entry)
            self._bump(cache_hits=1, tokens_saved=500)
            return r

        entry = self.store.fuzzy_lookup(intent.normalized)
        if entry and entry.action.get("type") == "llm_response":
            print(f"[FUZZY] '{intent.normalized}' → cached LLM")
            r = self._serve_cached_llm(entry)
            self._bump(cache_hits=1, tokens_saved=500)
            return r

        entry = self.store.semantic_lookup(intent.normalized)
        if entry and _category_mismatch(intent.normalized, entry.action.get("type")):
            print(f"[SEMANTIC] rejected cross-topic match: '{intent.normalized}' vs {entry.action.get('type')}")
            entry = None
        if entry:
            r = self._serve_cached_llm(entry)
            self._bump(cache_hits=1, tokens_saved=500)
            return r

        return self._call_llm(intent, history)

    def _exec_cached(self, entry: CacheEntry, fp, params=None) -> Optional[Response]:
        action = _substitute(entry.action, params or {})
        if action.get("type") in cfg.DISABLED_ACTIONS:
            return Response(f"Action '{action.get('type')}' is disabled.", source="blocked")
        if action.get("type") == "llm_response":
            return self._serve_cached_llm(entry)
        resp = self._exec_hands(action)
        is_adb = action.get("type") == "adb_command"
        if not is_adb and _is_garbage(resp.content):
            print(f"[EVICT] garbage → evicting {fp}")
            if fp: self.store.evict(fp)
            return None
        if is_adb and resp.content.strip().lower() in _BARE_OK:
            resp = Response(action.get("description") or "Done", source="cache")
        return resp

    def _serve_cached_llm(self, entry: CacheEntry) -> Response:
        a = entry.action
        if a.get("type") == "llm_response":
            return Response(a.get("full_response", a.get("description","...")), source="cache_hit")
        return self._exec_hands(a)

    def _try_open_unknown(self, query: str) -> Optional[Response]:
        m = re.match(r"^(?:open|launch|start|run)\s+(.+)$", query.strip(), re.I)
        if not m: return None
        name = re.sub(r"\s+(app|application|program)$", "", m.group(1), flags=re.I).strip().lower()
        try:
            from platforms.android.resolver import AppResolver
            r = AppResolver(); r.resolve()
            print(f"[RESOLVE] '{name}' in {len(r._installed)} packages")
            if not r.query_ok:
                return Response(
                    f"Can't check whether '{name}' is installed -- ADB isn't reachable right now "
                    "(phone disconnected/wireless adb dropped), not a real \"not found\".",
                    source="device_search_unavailable",
                )
            pkg = r.resolve_unknown(name)
            print(f"[RESOLVE] → {pkg}")
            if not pkg:
                return Response(f"Couldn't find '{name}' installed.", source="device_search")
            action = {"type":"open_app","description":f"Open {name.title()}","params":{"package":pkg,"app_name":name.title()}}
            resp = self._exec_hands(action)
            if not _is_garbage(resp.content):
                self.store.learn(fp=fingerprint(query,cfg.PLATFORM), intent_text=query, action=action, platform=cfg.PLATFORM)
                print(f"[LEARN] cached open_{name} → {pkg}")
            return resp
        except Exception:
            import traceback; traceback.print_exc()
            return None

    def _llm_passthrough(self, intent: Intent, history: list) -> Response:
        try:
            text, tokens = self.llm.complete(intent.raw, history=history)
            tokens = int(tokens or 0)
        except Exception as e:
            return Response(f"LLM error: {e}", source="llm_error")
        self._bump(tokens_spent=tokens)
        # No-cache intents (time-sensitive/creative/convo) still deserve real
        # device-action execution if the LLM returned one -- only the caching
        # step should be skipped here, not the action-detection/execution step.
        action = _extract_action(text)
        if action:
            print(f"[LLM→ACTION] (no-cache) type={action.get('type')}")
            content = self._run_action(action)
            return Response(content, tokens_spent=tokens, source="llm_action")
        return Response(text, tokens_spent=tokens, source="llm_passthrough")

    def _call_llm(self, intent: Intent, history: list) -> Response:
        try:
            text, tokens = self.llm.complete(intent.raw, history=history)
            tokens = int(tokens or 0)
        except Exception as e:
            return Response(f"LLM error: {e}", source="llm_error")
        self._bump(tokens_spent=tokens)
        print(f"[LLM] {text[:80]!r}")
        action = _extract_action(text)
        if action:
            print(f"[LLM→ACTION] type={action.get('type')}")
            return self._exec_llm_action(action, intent, tokens)
        # Cache plain text -- but not app+typing-verb prompts
        norm = intent.normalized
        if intent.cacheable and not (
            any(app in norm for app in _APP_TYPING_APPS) and
            any(v in norm for v in ("type","say","message","enter","write"))
        ):
            self.store.learn(
                fp=intent.fingerprint, intent_text=norm,
                action={"type":"llm_response","full_response":text,"description":text[:100],"original_prompt":norm},
                platform=cfg.PLATFORM,
            )
        return Response(text, tokens_spent=tokens, source="llm")

    def _exec_llm_action(self, action: dict, intent: Intent, tokens: int) -> Response:
        content = self._run_action(action)
        if content and not _is_garbage(content):
            self.store.learn(fp=intent.fingerprint, intent_text=intent.normalized, action=action, platform=cfg.PLATFORM)
            print(f"[LEARN] cached LLM action: {action.get('type')}")
        return Response(content, tokens_spent=tokens, source="llm_action")

    def _exec_hands(self, action) -> Response:
        action = _normalize_action(action)
        atype = action.get("type","") if isinstance(action,dict) else ""
        if atype in cfg.DISABLED_ACTIONS:
            return Response(f"Action '{atype}' is disabled.", source="blocked")
        if atype in ("adb_command", "run_command"):
            cmd = action.get("params", {}).get("cmd", "") if isinstance(action, dict) else ""
            if _is_destructive_command(cmd):
                print(f"[BLOCKED] destructive command refused: {cmd!r}")
                return Response(
                    "Refused: that looks like a destructive command (data/device wipe). "
                    "Not executing it. Ask again more specifically if this wasn't the intent.",
                    source="blocked",
                )
        if atype == "adb_command":
            cmd = action.get("params",{}).get("cmd","")
            action = {**action, "params":{**action.get("params",{}), "cmd":_fix_brightness(cmd)}}
        result = self.hands.execute(action)
        if not result.success:
            return Response(result.error or "Action failed.", source="hands_error")
        raw = (result.output or "").strip()
        if not raw:
            if atype in _QUERY_TYPES:
                return Response(f"[{atype} returned no data -- device may not have reported a value]", source="hands_error")
            return Response(_confirm(atype), source="hands")
        return Response(_format(atype, raw), source="hands")

    def _run_action(self, action) -> str:
        if isinstance(action, list):
            parts = []
            for step in action:
                r = self._exec_hands(step)
                parts.append(r.content if r.source != "hands_error" else f"[{step.get('type','?')} failed]")
                time.sleep(0.2)
            return " → ".join(parts)
        if isinstance(action, dict) and action.get("type") == "workflow":
            parts = []
            for step in action.get("steps",[]):
                if step.get("type") == "wait":
                    secs = float(step.get("params",{}).get("seconds",1))
                    time.sleep(min(secs,10))
                    parts.append(f"Waited {secs}s")
                    continue
                r = self._exec_hands(step)
                if r.source == "hands_error":
                    parts.append(f"[{step.get('type','?')} failed: {r.content}]")
                else:
                    c = r.content
                    if not c or c.lower().strip() in _BARE_OK:
                        c = step.get("description") or _confirm(step.get("type",""))
                    parts.append(c)
                time.sleep(0.3)
            return " → ".join(parts)
        r = self._exec_hands(action)
        c = r.content
        if isinstance(action,dict) and action.get("type") == "adb_command" and c.strip().lower() in _BARE_OK:
            c = action.get("description") or "Done"
        return c

    def ui_find(self, goal: str) -> Optional[tuple]:
        """
        Dump UI XML → send to LLM → get (x,y) to tap.
        Used for intelligent element finding without vision model.
        """
        try:
            r = self.hands.execute({"type":"dump_ui","params":{}})
            if not r.success or not r.output: return None
            xml = r.output[:8000]
            prompt = (
                f"Given this Android UI XML, what are the x,y pixel coordinates "
                f"of the center of the element I should tap to: {goal}\n\n"
                f"XML:\n{xml}\n\n"
                f"Reply with ONLY: x,y (two integers). If not found reply: not_found"
            )
            text, _ = self.llm.complete(prompt)
            text = text.strip()
            if text == "not_found": return None
            parts = text.split(",")
            if len(parts) == 2:
                return int(parts[0].strip()), int(parts[1].strip())
        except Exception as e:
            print(f"[UI_FIND] {e}")
        return None

    def _log(self, intent: Intent, resp: Response):
        try:
            with open(cfg.LOG_PATH, "a") as f:
                f.write(json.dumps({
                    "ts":intent.normalized[:40],"kind":intent.kind,
                    "source":resp.source,"tokens":resp.tokens_spent,"saved":self._tokens_saved,
                }) + "\n")
        except Exception: pass

    def stats(self) -> dict:
        return {
            "requests":     self._requests,
            "cache_hits":   self._cache_hits,
            "hit_rate":     f"{round(self._cache_hits / max(self._requests,1) * 100)}%",
            "tokens_spent": self._tokens_spent,
            "tokens_saved": self._tokens_saved,
        }
