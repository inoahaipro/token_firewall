"""
server.py: Token Firewall v3
FastAPI async server. OpenAI-compatible API + built-in chat UI.

Fixes vs previous:
  - /v1/screen uses module-level hands/store (not undefined vars)
  - /v1/export-pack uses store.export() method not direct _db access
  - CDP import removed (doesn't work on mobile)
  - Routes defined BEFORE entry point
  - Conversation memory: last N messages passed to LLM
  - Auto-promote high-hit entries on each request
"""
import asyncio
import json
import time
import sys
import traceback
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core.config as cfg

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
    import uvicorn
except ImportError:
    print("Missing deps. Run: pip install fastapi 'uvicorn[standard]' --break-system-packages")
    sys.exit(1)

from core.cache.store import KnowledgeStore
from core.intent.engine import IntentEngine
from core.dispatch.router import FirewallRouter
from adapters.llm import LLMAdapter


# ── Platform hands loader ─────────────────────────────────────────────────────

class CompositeHands:
    """Desktop hands (this machine) + rish/SSH phone hands (remote Android),
    both live simultaneously since TF runs on the PC but can drive either."""

    def __init__(self, *sub_hands):
        self._subs = list(sub_hands)
        self.platform_id = "+".join(h.platform_id for h in self._subs)

    def capabilities(self):
        caps = []
        for h in self._subs:
            caps.extend(h.capabilities())
        return caps

    def can_execute(self, action):
        return any(h.can_execute(action) for h in self._subs)

    def execute(self, action):
        for h in self._subs:
            if h.can_execute(action):
                return h.execute(action)
        from core.hands.base import ActionResult
        return ActionResult(False, error=f"no hands claim action: {action.get('type')}")


def _load_hands():
    p = cfg.PLATFORM
    if p == "android":
        from platforms.android.hands import AndroidHands
        return AndroidHands()
    elif p in ("linux", "macos", "windows"):
        try:
            from platforms.desktop.hands import DesktopHands
            from platforms.rish_phone.hands import RishPhoneHands
            return CompositeHands(DesktopHands(), RishPhoneHands())
        except ImportError:
            return _NoHands(p)
    elif p == "ios":
        try:
            from platforms.ios.hands import IOSHands
            return IOSHands()
        except ImportError:
            return _NoHands(p)
    return _NoHands(p)


class _NoHands:
    def __init__(self, p="unknown"):
        self.platform_id = p
        print(f"[WARN] No hands for platform: {p}")
    def can_execute(self, a): return False
    def capabilities(self): return []
    def execute(self, a):
        from core.hands.base import ActionResult
        return ActionResult(success=False, error=f"No hands for: {self.platform_id}")


# ── Module-level singletons ───────────────────────────────────────────────────

_start_time = int(time.time())
store  = KnowledgeStore()
engine = IntentEngine()
hands  = _load_hands()
llm    = LLMAdapter()
router = FirewallRouter(store, engine, hands, llm)

app = FastAPI(title="Token Firewall", version="3.0", docs_url=None, redoc_url=None)

# router.route() is fully synchronous blocking I/O (SQLite, subprocess SSH
# calls, LLM HTTP requests) called from an async route with no thread
# offload, so it fully blocks the single event loop for the whole call --
# every request already serializes in practice. Found live under stress
# testing: a burst of concurrent requests queued up unbounded, 40% of a
# 10-request burst silently timed out client-side (each request waiting
# behind however many arrived before it), and the process was SIGKILLed
# once during the test window. This doesn't add real parallelism (that
# would need a bigger async/threading rework) -- it caps how many requests
# get admitted to the queue at all, so excess load gets a fast, honest 503
# instead of hanging past the caller's own timeout with no explanation.
_REQUEST_GATE = asyncio.Semaphore(3)
_GATE_WAIT_S = 5


# ── Prompt + history extraction ───────────────────────────────────────────────

def _extract_prompt(messages: list) -> str:
    """Get last user message, strip OpenClaw metadata."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text","") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
            content = content or ""
            if "```" in content:
                content = content.split("```")[-1]
            content = re.sub(r"^\[[^\]]+\]\s*", "", (content or "").strip())
            return content.strip()
    return ""


def _extract_history(messages: list, max_turns: int = 6) -> list:
    """
    Extract last N conversation turns for LLM context (conversation memory).
    Strips OpenClaw metadata, keeps only user/assistant pairs.
    """
    clean = []
    for msg in messages:
        role    = msg.get("role", "")
        content = msg.get("content", "")
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, list):
            content = " ".join(
                p.get("text","") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        if content and "```" in content:
            content = content.split("```")[-1]
        content = re.sub(r"^\[[^\]]+\]\s*", "", (content or "").strip())
        if content:
            clean.append({"role": role, "content": content})
    # Keep last max_turns messages (user+assistant pairs)
    return clean[-max_turns:] if len(clean) > max_turns else clean


# ── SSE ───────────────────────────────────────────────────────────────────────

def _sse(content: str, tokens: int):
    rid = f"tf-{int(time.time())}"
    ts  = int(time.time())
    def _gen():
        yield "data: " + json.dumps({
            "id":rid,"object":"chat.completion.chunk","created":ts,"model":"token-firewall",
            "choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":None}],
        }) + "\n\n"
        yield "data: " + json.dumps({
            "id":rid,"object":"chat.completion.chunk","created":ts,"model":"token-firewall",
            "choices":[{"index":0,"delta":{"content":content},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":tokens,"completion_tokens":len(content.split()),"total_tokens":tokens},
        }) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"},
    )


# ── Auto-promote helper ───────────────────────────────────────────────────────

_last_promote = 0

def _maybe_promote():
    """Promote high-hit learned entries to pack every 10 minutes."""
    global _last_promote
    now = time.time()
    if now - _last_promote < 600:
        return
    _last_promote = now
    try:
        rows = store._db.execute(
            "SELECT * FROM learned WHERE hits >= ? ORDER BY hits DESC",
            (cfg.CACHE_PROMOTE_HITS,)
        ).fetchall()
        if not rows:
            return
        pack_file = cfg.PACK_DIR / cfg.PLATFORM / "learned_promoted.json"
        pack_file.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if pack_file.exists():
            try:
                existing = json.loads(pack_file.read_text())
            except Exception:
                pass
        existing_intents = {e.get("intent","") for e in existing}
        added = 0
        for r in rows:
            intent = r["intent_text"] or ""
            if intent and intent not in existing_intents:
                existing.append({
                    "intent":     intent,
                    "action":     json.loads(r["action_json"]),
                    "confidence": r["confidence"],
                    "platform":   r["platform"],
                })
                existing_intents.add(intent)
                added += 1
        if added:
            pack_file.write_text(json.dumps(existing, indent=2))
            print(f"[PROMOTE] {added} entries promoted to {pack_file.name}")
    except Exception as e:
        print(f"[PROMOTE] error: {e}")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
@app.get("/v1/health")
async def health():
    import subprocess
    adb_ok = termux_ok = False
    try:
        r = subprocess.run(["adb","devices"], capture_output=True, text=True, timeout=3)
        # adb devices' own header is "List of devices attached", which
        # contains the substring "device" even with zero devices connected --
        # a plain `"device" in stdout` check is always true. Match an actual
        # device line instead (format: "SERIAL\tdevice").
        adb_ok = bool(re.search(r"\t(device)\b", r.stdout))
    except Exception: pass
    try:
        r = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=3)
        termux_ok = r.returncode == 0
    except Exception: pass
    stats = router.stats()
    return {
        "status":   "ok",
        "platform": cfg.PLATFORM,
        "uptime_s": int(time.time()) - _start_time,
        "adb":      "ok" if adb_ok else "unavailable",
        "termux":   "ok" if termux_ok else "unavailable",
        "llm": {
            "url":       cfg.LLM_BASE_URL,
            "model":     cfg.LLM_MODEL,
            "providers": len(llm._providers),
        },
        "cache":  store.stats(),
        "router": stats,
        "tokens": cfg.token_stats(),
    }


@app.get("/v1/models")
async def models():
    return {"object":"list","data":[{
        "id":"token-firewall","object":"model","created":_start_time,
        "description":"Token Firewall v3: zero-token cached device gateway",
    }]}


@app.get("/v1/capabilities")
async def capabilities():
    s = store.stats()
    return {
        "platform":     cfg.PLATFORM,
        "capabilities": hands.capabilities(),
        "cache_entries": s.get("pack_entries",0) + s.get("learned_entries",0),
        "disabled":     list(cfg.DISABLED_ACTIONS),
    }


@app.post("/v1/chat/completions")
async def completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error":"invalid JSON"}, status_code=400)

    messages = body.get("messages", [])
    if not messages:
        return JSONResponse({"error":"no messages"}, status_code=400)

    prompt  = _extract_prompt(messages)
    history = _extract_history(messages)
    stream  = body.get("stream", False)

    print(f"[REQ] stream={stream} msgs={len(messages)} {prompt[:60]!r}")

    try:
        await asyncio.wait_for(_REQUEST_GATE.acquire(), timeout=_GATE_WAIT_S)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "too many concurrent requests, try again shortly"},
            status_code=503,
        )
    try:
        # Offloaded to a worker thread -- router.route() is fully
        # synchronous blocking I/O (SQLite, subprocess SSH, LLM HTTP calls).
        # Calling it directly here blocks the WHOLE event loop for the
        # entire request, which meant no other coroutine -- including the
        # request-gate's own timeout timer above -- could run until it
        # finished. Found live: that's why the gate did nothing under real
        # concurrent load; the event loop was too wedged to ever notice a
        # timeout. to_thread() actually frees the loop to stay responsive.
        result = await asyncio.to_thread(router.route, prompt, history)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error":str(e)}, status_code=500)
    finally:
        _REQUEST_GATE.release()

    content = str(result.content or "")
    print(f"[RESP] source={result.source} tokens={result.tokens_spent} {content[:60]!r}")

    # Background: promote high-hit learned entries to pack
    _maybe_promote()

    if stream:
        return _sse(content, result.tokens_spent)

    return {
        "id":      f"tf-{int(time.time())}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   "token-firewall",
        "choices": [{"index":0,"message":{"role":"assistant","content":content},"finish_reason":"stop"}],
        # result.tokens_spent is already a real token count reported by the
        # LLM provider when a real call happened (adapters/llm.py reads it
        # straight from the API's own usage.total_tokens), and genuinely 0
        # on a cache hit -- no LLM call, no tokens spent. Adding
        # len(content.split()) on top was padding an already-real number
        # with a fake word count, which is why cache hits showed "2 tokens"
        # instead of the true 0.
        "usage":   {
            "prompt_tokens":     result.tokens_spent,
            "completion_tokens": result.tokens_spent,
            "total_tokens":      result.tokens_spent,
        },
    }


@app.post("/v1/tools/execute")
async def tool_execute(request: Request):
    """
    Execute a device action directly (for MCP tool calls and OpenClaw agent use).
    Body: {"action": {"type": "open_app", "params": {...}}}
       or {"prompt": "open spotify"}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error":"invalid JSON"}, status_code=400)

    action = body.get("action", {})
    if not action:
        prompt = body.get("prompt","")
        if prompt:
            result = router.route(prompt)
            return {"success":True, "output":result.content, "source":result.source}
        return JSONResponse({"error":"action or prompt required"}, status_code=400)

    # Route through the router's _exec_hands, not hands.execute() directly --
    # calling hands directly here completely bypassed the destructive-command
    # guardrail and the TF_DISABLE_ACTIONS check, both of which only live in
    # _exec_hands. Found live during a security review: this endpoint is
    # explicitly documented as "for MCP tool calls and OpenClaw agent use",
    # i.e. exactly the path an AI assistant's tool layer would hit, so
    # bypassing the guard here defeated it entirely regardless of what got
    # fixed in the router.
    result = router._exec_hands(action)
    return {"success": result.source not in ("hands_error", "blocked"), "output": result.content, "source": result.source}


@app.post("/v1/ui-find")
async def ui_find(request: Request):
    """
    Dump screen UI XML, send to LLM, get back (x,y) to tap.
    Body: {"goal": "the search button"}
    Returns: {"x": 540, "y": 120} or 404
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error":"invalid JSON"}, status_code=400)
    goal = body.get("goal","")
    if not goal:
        return JSONResponse({"error":"goal required"}, status_code=400)
    coords = router.ui_find(goal)
    if coords:
        return {"x":coords[0], "y":coords[1]}
    return JSONResponse({"error":"not found"}, status_code=404)


@app.get("/v1/screen")
async def screen_state():
    """
    Get current screen state: current app, UI XML (full, not truncated), browser URL.
    Used by Codex and agents for screen-aware automation.
    """
    import subprocess
    state = {"platform": cfg.PLATFORM}

    # Current app via ADB
    try:
        r = subprocess.run(
            ["adb","shell","dumpsys","activity","activities"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            if "mResumedActivity" in line:
                state["current_app"] = line.strip()
                break
    except Exception: pass

    # Full UI dump via uiautomator2 if available, else raw ADB
    try:
        from platforms.android import u2
        if u2.is_available():
            xml = u2.get_screen_xml()
            if xml:
                state["ui_xml"] = xml
                state["ui_source"] = "uiautomator2"
    except Exception: pass

    if "ui_xml" not in state:
        try:
            subprocess.run(["adb","shell","uiautomator","dump","/sdcard/ui_state.xml"],
                          capture_output=True, timeout=5)
            r2 = subprocess.run(["adb","shell","cat","/sdcard/ui_state.xml"],
                                capture_output=True, text=True, timeout=5)
            if r2.returncode == 0:
                state["ui_xml"] = r2.stdout  # full, not truncated
                state["ui_source"] = "uiautomator_raw"
        except Exception: pass

    # Element count for quick sanity check
    if "ui_xml" in state:
        state["element_count"] = state["ui_xml"].count("<node ")

    return state


@app.post("/v1/export-pack")
async def export_pack(request: Request):
    """Export high-hit learned entries to a knowledge pack JSON."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    min_hits = int(body.get("min_hits", 5))
    # The requested output path is unvalidated user input -- constrain it to
    # stay inside packs/, resolving any ../ traversal or absolute-path
    # attempt first. Found live during a security review: this was an
    # arbitrary-file-write primitive (any path, parent dirs auto-created,
    # content is DB-derived JSON but the DESTINATION was fully attacker-
    # controlled -- e.g. could target ~/.ssh/authorized_keys).
    packs_base = (Path(__file__).resolve().parent / "packs").resolve()
    requested = body.get("output", f"{cfg.PLATFORM}/exported.json")
    output = (packs_base / requested).resolve()
    if packs_base not in output.parents and output != packs_base:
        return JSONResponse({"error": "output path must stay inside packs/"}, status_code=400)
    try:
        rows = store._db.execute(
            "SELECT * FROM learned WHERE hits >= ? ORDER BY hits DESC",
            (min_hits,)
        ).fetchall()
        pack = [{
            "intent":     r["intent_text"] or "",
            "action":     json.loads(r["action_json"]),
            "confidence": r["confidence"],
            "platform":   r["platform"],
        } for r in rows if r["intent_text"]]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(pack, indent=2))
        return {"exported":len(pack), "path":str(output), "min_hits":min_hits}
    except Exception as e:
        return JSONResponse({"error":str(e)}, status_code=500)


# ── Built-in chat UI ──────────────────────────────────────────────────────────

_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Token Firewall</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0a;color:#e4e4e7;height:100dvh;display:flex;flex-direction:column;overflow:hidden}
#header{padding:12px 16px;border-bottom:1px solid #1f1f1f;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-shrink:0}
#header h1{font-size:16px;font-weight:600;color:#fff}
#hstats{font-size:11px;color:#52525b;flex:1;text-align:right}
#msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.bubble{display:flex;flex-direction:column;gap:3px}
.bubble.user{align-items:flex-end}
.bubble.bot{align-items:flex-start}
.msg{max-width:84%;padding:10px 14px;border-radius:18px;line-height:1.55;white-space:pre-wrap;word-break:break-word;font-size:15px}
.user .msg{background:#2563eb;color:#fff;border-bottom-right-radius:4px}
.bot .msg{background:#18181b;border:1px solid #27272a;border-bottom-left-radius:4px}
.bot .msg.thinking{opacity:.4}
.src{font-size:10px;color:#3f3f46;padding:0 4px}
.src.cache{color:#16a34a}
.src.llm{color:#ea580c}
.src.hands{color:#7c3aed}
#form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid #1f1f1f;background:#0a0a0a;flex-shrink:0}
#inp{flex:1;background:#18181b;border:1px solid #27272a;border-radius:14px;padding:10px 14px;color:#e4e4e7;font-size:15px;resize:none;max-height:120px;outline:none;line-height:1.4;font-family:inherit}
#inp:focus{border-color:#2563eb}
#btn{background:#2563eb;border:none;border-radius:14px;padding:10px 18px;color:#fff;font-size:15px;cursor:pointer;flex-shrink:0}
#btn:disabled{opacity:.4}
</style>
</head>
<body>
<div id="header">
  <h1>⚡ Token Firewall</h1>
  <span id="hstats">loading…</span>
</div>
<div id="msgs">
  <div class="bubble bot"><div class="msg">Ready. Ask anything or give a device command.</div></div>
</div>
<form id="form">
  <textarea id="inp" rows="1" placeholder="Message or command…"></textarea>
  <button id="btn" type="submit">↑</button>
</form>
<script>
const msgs=document.getElementById('msgs'),
      inp=document.getElementById('inp'),
      btn=document.getElementById('btn'),
      hstats=document.getElementById('hstats');
let history=[];

function srcClass(s){
  if(!s)return '';
  if(s.includes('cache')||s.includes('hit'))return 'cache';
  if(s.includes('llm'))return 'llm';
  if(s.includes('hands'))return 'hands';
  return '';
}
function srcLabel(s){
  if(!s)return '';
  const map={cache_hit:'⚡ cache',hands:'🤖 device',chain:'🔗 chain',
             llm:'🧠 llm',llm_action:'🧠→device',llm_passthrough:'🧠 llm',
             device_search:'🔍 search'};
  return map[s]||s;
}

function addMsg(text,role,source){
  const wrap=document.createElement('div');
  wrap.className='bubble '+role;
  const d=document.createElement('div');
  d.className='msg'; d.textContent=text;
  wrap.appendChild(d);
  if(source&&role==='bot'){
    const s=document.createElement('div');
    s.className='src '+srcClass(source);
    s.textContent=srcLabel(source);
    wrap.appendChild(s);
  }
  msgs.appendChild(wrap);
  msgs.scrollTop=msgs.scrollHeight;
  return d;
}

inp.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();document.getElementById('form').requestSubmit();}
});
inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,120)+'px';});

async function fetchStats(){
  try{
    const d=await fetch('/health').then(r=>r.json());
    const ro=d.router||{};
    hstats.textContent=`⚡${ro.cache_hits||0} hits · 🧠${ro.tokens_spent||0} tokens · ${ro.hit_rate||'-'}`;
  }catch{}
}

document.getElementById('form').addEventListener('submit',async e=>{
  e.preventDefault();
  const text=inp.value.trim(); if(!text)return;
  inp.value=''; inp.style.height='auto'; btn.disabled=true;
  addMsg(text,'user');
  history.push({role:'user',content:text});
  const botMsg=addMsg('…','bot'); botMsg.classList.add('thinking');
  let srcDiv=null;
  try{
    const resp=await fetch('/v1/chat/completions',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:'token-firewall',messages:history,stream:true})
    });
    const reader=resp.body.getReader(),dec=new TextDecoder();
    let full='', lastSource='';
    while(true){
      const{done,value}=await reader.read(); if(done)break;
      for(const line of dec.decode(value).split('\\n')){
        if(!line.startsWith('data:')||line.includes('[DONE]'))continue;
        try{
          const d=JSON.parse(line.slice(5));
          const delta=d.choices?.[0]?.delta?.content||'';
          if(delta){full+=delta;botMsg.textContent=full;botMsg.classList.remove('thinking');msgs.scrollTop=msgs.scrollHeight;}
          if(d.source) lastSource=d.source;
        }catch{}
      }
    }
    // Add source label after streaming
    const wrap=botMsg.closest('.bubble');
    if(wrap&&lastSource){
      const s=document.createElement('div');
      s.className='src '+srcClass(lastSource);
      s.textContent=srcLabel(lastSource);
      wrap.appendChild(s);
    }
    history.push({role:'assistant',content:full});
  }catch(err){botMsg.textContent='Error: '+err.message;}
  finally{btn.disabled=false;inp.focus();fetchStats();}
});

fetchStats(); setInterval(fetchStats,15000);
</script>
</body>
</html>"""


@app.get("/")
@app.get("/ui")
async def ui():
    html_file = Path(__file__).parent / "ui" / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text())
    return HTMLResponse(_UI_HTML)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  Token Firewall v3: {cfg.PLATFORM}")
    print(f"  API    : http://{cfg.HOST}:{cfg.PORT}/v1/chat/completions")
    print(f"  UI     : http://{cfg.HOST}:{cfg.PORT}/ui")
    print(f"  Health : http://{cfg.HOST}:{cfg.PORT}/health")
    print(f"  Screen : http://{cfg.HOST}:{cfg.PORT}/v1/screen")
    print(f"  LLM    : {cfg.LLM_BASE_URL or '(not set)'} ({cfg.LLM_MODEL})")
    print(f"  DB     : {cfg.DB_PATH}")
    print(f"  Packs  : {cfg.PACK_DIR}")
    if cfg.LLM_FALLBACKS:
        count = len([x for x in cfg.LLM_FALLBACKS.split(";") if x.strip()])
        print(f"  LLM fallbacks: {count}")
    if cfg.DISABLE_ADB:    print("  [ADB disabled]")
    if cfg.DISABLE_TERMUX: print("  [Termux disabled]")
    print()

    uvicorn.run(
        "server:app",
        host=cfg.HOST,
        port=cfg.PORT,
        log_level="warning",
        access_log=False,
        reload=False,
    )
