"""
core/dispatch/router.py — FirewallRouter v3

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

def _confirm(atype: str) -> str:
    return {
        "vibrate":"Done — vibrated.","torch":"Done — flashlight toggled.",
        "take_photo":"Done — photo taken.","send_sms":"Done — SMS sent.",
        "open_app":"Done — app opened.","close_app":"Done — app closed.",
        "key_event":"Done.","tap":"Done — tapped.","long_press":"Done — long pressed.",
        "swipe":"Done — swiped.","scroll_up":"Done — scrolled up.",
        "scroll_down":"Done — scrolled down.","type_text":"Done — typed.",
        "screenshot_adb":"Done — screenshot saved.","clipboard_set":"Done — copied.",
        "find_and_tap":"Done — tapped element.","find_and_type":"Done — typed into field.",
        "find_and_scroll":"Done — scrolled.","adb_command":"Done.","wait":"Done — waited.",
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
        if status == "full": return f"Battery {pct}% — fully charged{plug}. {tail}"
        elif status == "charging": return f"Battery {pct}% — charging{plug}. {tail}"
        else: return f"Battery {pct}% — discharging. {tail}"
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
        self._history      = []

    def route(self, prompt: str, history: list = None) -> Response:
        self._requests += 1
        self._history = history or []
        intent = self.engine.process(prompt)
        if intent.kind == CHAIN and intent.sub_intents:
            resp = self._route_chain(intent)
        else:
            resp = self._route_one(intent)
        self._log(intent, resp)
        return resp

    def _route_chain(self, chain: Intent) -> Response:
        parts, total = [], 0
        for sub in chain.sub_intents:
            r = self._route_one(sub)
            total += r.tokens_spent
            parts.append(r.content if r.source not in ("hands_error","llm_error") else f"[{sub.normalized}: failed]")
            time.sleep(0.3)
        return Response(" → ".join(parts), tokens_spent=total, source="chain")

    def _route_one(self, intent: Intent) -> Response:
        print(f"[ROUTE] {intent.kind} {intent.normalized!r}")

        if intent.kind == DEVICE:
            entry = self.store.lookup(intent.fingerprint)
            if entry:
                r = self._exec_cached(entry, intent.fingerprint, intent.params)
                if r is not None:
                    self._cache_hits += 1; self._tokens_saved += 500
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
                    self._cache_hits += 1; self._tokens_saved += 500
                    return r

            entry = self.store.semantic_lookup(intent.normalized)
            if entry and _category_mismatch(intent.normalized, entry.action.get("type")):
                print(f"[SEMANTIC] rejected cross-topic match: '{intent.normalized}' vs {entry.action.get('type')}")
                entry = None
            if entry:
                r = self._exec_cached(entry, None, intent.params)
                if r is not None:
                    self._cache_hits += 1; self._tokens_saved += 500
                    return r

            if re.match(r"^(?:open|launch|start|run)\s+.+", intent.normalized, re.I):
                r = self._try_open_unknown(intent.normalized)
                if r is not None: return r

            return self._call_llm(intent)

        # LEARNED / LLM
        if (not intent.cacheable
                or _NO_CACHE_TIME.search(intent.normalized)
                or _NO_CACHE_CREATIVE.search(intent.normalized)
                or _NO_CACHE_CONVO.search(intent.normalized)):
            return self._llm_passthrough(intent)

        entry = self.store.lookup(intent.fingerprint)
        if entry:
            r = self._serve_cached_llm(entry)
            self._cache_hits += 1; self._tokens_saved += 500
            return r

        entry = self.store.fuzzy_lookup(intent.normalized)
        if entry and entry.action.get("type") == "llm_response":
            print(f"[FUZZY] '{intent.normalized}' → cached LLM")
            r = self._serve_cached_llm(entry)
            self._cache_hits += 1; self._tokens_saved += 500
            return r

        entry = self.store.semantic_lookup(intent.normalized)
        if entry and _category_mismatch(intent.normalized, entry.action.get("type")):
            print(f"[SEMANTIC] rejected cross-topic match: '{intent.normalized}' vs {entry.action.get('type')}")
            entry = None
        if entry:
            r = self._serve_cached_llm(entry)
            self._cache_hits += 1; self._tokens_saved += 500
            return r

        return self._call_llm(intent)

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

    def _llm_passthrough(self, intent: Intent) -> Response:
        try:
            text, tokens = self.llm.complete(intent.raw, history=self._history)
            tokens = int(tokens or 0)
        except Exception as e:
            return Response(f"LLM error: {e}", source="llm_error")
        self._tokens_spent += tokens
        # No-cache intents (time-sensitive/creative/convo) still deserve real
        # device-action execution if the LLM returned one -- only the caching
        # step should be skipped here, not the action-detection/execution step.
        action = _extract_action(text)
        if action:
            print(f"[LLM→ACTION] (no-cache) type={action.get('type')}")
            content = self._run_action(action)
            return Response(content, tokens_spent=tokens, source="llm_action")
        return Response(text, tokens_spent=tokens, source="llm_passthrough")

    def _call_llm(self, intent: Intent) -> Response:
        try:
            text, tokens = self.llm.complete(intent.raw, history=self._history)
            tokens = int(tokens or 0)
        except Exception as e:
            return Response(f"LLM error: {e}", source="llm_error")
        self._tokens_spent += tokens
        print(f"[LLM] {text[:80]!r}")
        action = _extract_action(text)
        if action:
            print(f"[LLM→ACTION] type={action.get('type')}")
            return self._exec_llm_action(action, intent, tokens)
        # Cache plain text — but not app+typing-verb prompts
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
        atype = action.get("type","") if isinstance(action,dict) else ""
        if atype in cfg.DISABLED_ACTIONS:
            return Response(f"Action '{atype}' is disabled.", source="blocked")
        if atype == "adb_command":
            cmd = action.get("params",{}).get("cmd","")
            action = {**action, "params":{**action.get("params",{}), "cmd":_fix_brightness(cmd)}}
        result = self.hands.execute(action)
        if not result.success:
            return Response(result.error or "Action failed.", source="hands_error")
        raw = (result.output or "").strip()
        return Response(_format(atype, raw) if raw else _confirm(atype), source="hands")

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
