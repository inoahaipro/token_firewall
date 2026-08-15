"""
imprint.py: Local Agentic Model (LAM) for OpenClaw
=====================================================
v2.0.0

The core idea:
  LLM teaches once. IMPRINT executes forever after. Zero tokens for known tasks.

What's in v2.0.0:
  - Parameterized skills: send_message(contact, message) not hardcoded plans
  - Element-based targeting: tap by text/id/desc, not raw coordinates
  - Confirmation threshold: N successes required before trusting a plan
  - Per-step retry logic with structured results
  - Mid-task replan: step failure escalates to LLM with current state
  - Active screen transition detection after every tap
  - Keyboard/focus detection before typing
  - Task timeout + max steps guard
  - Installed apps auto-discovery
  - Debug mode logging

How it works:
  1. Intent arrives → param extraction strips variables
  2. IMPRINT searches cache using normalized template + local TF-IDF
  3. HIT (trusted)  → resolve params → element-based execution → verify each step
  4. HIT (pending)  → execute → increment confirm count → promote at threshold
  5. MISS           → LLM plans abstract steps → execute → store as pending

No hallucinations: IMPRINT never generates actions. It only executes stored
procedures validated by LLM and confirmed through repeated real-world success.

USAGE:
  python imprint.py ask "message mom on WhatsApp hey"
  python imprint.py ask "turn on wifi" --dry
  python imprint.py stats
  python imprint.py list
  python imprint.py forget "message mom"
  python imprint.py apps
  python imprint.py check

ENV VARS:
  OPENCLAW_SESSION    OpenClaw session to use for planning (default "main")
  ADB_PORT            Default 34371 (OpenClaw Termux: 34371)
  IMPRINT_DB          Default ~/.imprint/memory.db
  IMPRINT_THRESHOLD   Similarity threshold (default 0.72)
  WRANGLE_PATH      Path to wrangle.py
  IMPRINT_CONFIRM     Successes before trusting plan (default 2)
  IMPRINT_MAX_STEPS   Max steps per task (default 20)
  IMPRINT_TIMEOUT     Task timeout seconds (default 120)
  IMPRINT_RETRIES     Retries per failed action (default 2)
  IMPRINT_DEBUG       Set 1 for verbose logging
"""

import os, re, sys, json, math, time, hashlib, sqlite3, subprocess, logging, urllib.parse
PYTHON = sys.executable  # always use the running interpreter, not bare "python"
from datetime import datetime
from collections import Counter

# ── Config ────────────────────────────────────────────────────────────────────

# OpenClaw agent session to use for LLM planning.
# Override with OPENCLAW_SESSION env var if you use a different session name.
OPENCLAW_SESSION = os.environ.get("OPENCLAW_SESSION", "main")
ADB_PORT     = os.environ.get("ADB_PORT", "45171")
DB_PATH      = os.environ.get("IMPRINT_DB", os.path.expanduser("~/.imprint/memory.db"))
THRESHOLD    = float(os.environ.get("IMPRINT_THRESHOLD", "0.72"))
WRANGLE      = os.environ.get("WRANGLE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrangle.py"))
MAX_FAILURES = 3
DECAY_DAYS   = 30
N_CONFIRM    = int(os.environ.get("IMPRINT_CONFIRM",   "2"))
MAX_STEPS    = int(os.environ.get("IMPRINT_MAX_STEPS", "20"))
TASK_TIMEOUT = int(os.environ.get("IMPRINT_TIMEOUT",   "120"))
MAX_RETRIES  = int(os.environ.get("IMPRINT_RETRIES",   "2"))
DEBUG        = os.environ.get("IMPRINT_DEBUG", "0") == "1"
VERSION      = "2.0.0"

# ── Error codes (use these, not raw strings) ──────────────────────────────────
ERR_ADB_UNAVAILABLE   = "adb_unavailable"
ERR_STATE_PARSE       = "state_parse_error"
ERR_TARGET_NOT_FOUND  = "target_not_found"
ERR_LAUNCH_FAILED     = "launch_failed"
ERR_INPUT_FAILED      = "input_failed"
ERR_SCREEN_DRIFT      = "screen_drift"
ERR_UNSAFE_BLOCKED    = "unsafe_action_blocked"
ERR_MAX_STEPS         = "max_steps_exceeded"
ERR_TIMEOUT           = "task_timeout"
ERR_NO_LLM_KEY        = "llm_key_missing"

logging.basicConfig(level=logging.DEBUG if DEBUG else logging.WARNING, format="[%(levelname)s] %(message)s")
log = logging.getLogger("imprint")

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS plans (
            id                   TEXT PRIMARY KEY,
            template             TEXT NOT NULL,
            intent_vec           TEXT NOT NULL,
            steps                TEXT NOT NULL,
            param_slots          TEXT DEFAULT '[]',
            context              TEXT,
            trusted              INTEGER DEFAULT 0,
            confirm_count        INTEGER DEFAULT 0,
            hits                 INTEGER DEFAULT 0,
            failures             INTEGER DEFAULT 0,
            consecutive_failures INTEGER DEFAULT 0,
            created_at           TEXT,
            last_used            TEXT,
            last_result          TEXT
        );
        CREATE TABLE IF NOT EXISTS step_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT,
            plan_id        TEXT,
            step_num       INTEGER,
            action         TEXT,
            target         TEXT,
            success        INTEGER,
            screen_changed INTEGER,
            error          TEXT,
            duration_ms    INTEGER
        );
        CREATE TABLE IF NOT EXISTS task_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            intent      TEXT,
            template    TEXT,
            source      TEXT,
            plan_id     TEXT,
            similarity  REAL,
            success     INTEGER,
            tokens_used INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            steps_taken INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT,
            intent     TEXT NOT NULL,
            dry_run    INTEGER DEFAULT 0,
            status     TEXT DEFAULT 'pending',
            result     TEXT,
            error      TEXT,
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS app_cache (
            package    TEXT PRIMARY KEY,
            name       TEXT,
            updated_at TEXT
        );
    """)
    # WAL mode: faster writes, safe for concurrent readers (OpenClaw dispatcher)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Indexes for query performance
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_plans_last_used ON plans(last_used DESC);
        CREATE INDEX IF NOT EXISTS idx_plans_trusted   ON plans(trusted, consecutive_failures);
        CREATE INDEX IF NOT EXISTS idx_task_log_ts     ON task_log(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_step_log_plan   ON step_log(plan_id);
        CREATE INDEX IF NOT EXISTS idx_queue_status    ON queue(status, id);
    """)
    conn.commit()
    # Seed built-in templates (e.g., Facebook post flow) if templates.json exists
    seed_templates(conn)
    return conn

# ── Param Extraction ──────────────────────────────────────────────────────────

# Param patterns: (regex, slot_name, keep_in_template)
# keep=False → replace match with {slot} so "msg Mom" and "msg Dad" share same template
# keep=True  → leave in template (used when value IS the distinguishing feature)
PARAM_PATTERNS = [
    # Quoted string → {message}
    (re.compile(r'"([^"]+)"'),                                               "message", False),
    (re.compile(r"'([^']+)'"),                                               "message", False),
    # "saying X" / "with message X" → {message}  (must come before contact)
    (re.compile(r'\b(?:saying|with message|with text)\s+(.+)$', re.I),    "message", False),
    # Social post patterns: "post hi on facebook" → {message}
    (re.compile(r'\bpost\s+(.+?)\s+on\s+facebook\b', re.I),             "message", False),
    # Search/play/watch/find queries → {query}
    # Captures: "search for X on Y", "play X on Y", "watch X on Y", "find X", "look up X"
    # Stops before " on ", " in ", " at " to avoid swallowing the app/platform name
    (re.compile(r'\b(?:search for|search|play|watch|find|look up|listen to)\s+(.+?)(?=\s+(?:on|in|at|via|using)\s+\w|\s*$)', re.I), "query", False),
    # Contact after action verb: captures Title Case AND lowercase names
    # Stops at prepositions (on, via, in, at, through) to avoid "Mom on WhatsApp" → "Mom on"
    # Examples: "message mom", "call John", "text Dr Smith"
    (re.compile(r'\b(?:message|call|text|dm|email)\s+((?:Dr\.?\s+)?\w+(?:\s+\w+)??)(?=\s+(?:on|via|in|at|through|using|with)|\s+saying|\s+with\s+|$)', re.I), "contact", False),
    # App name after open/launch/start (keep=False → becomes {app} in template)
    (re.compile(r'\b(?:open|launch|start|close|switch to)\s+(\w+)', re.I), "app", False),
]

def extract_params(intent):
    """Strip variable content → (template, params_dict)."""
    text = intent.strip()
    params = {}
    counts = Counter()
    for pattern, slot, keep in PARAM_PATTERNS:
        m = pattern.search(text)
        if m:
            val = m.group(1).strip()
            counts[slot] += 1
            key = slot if counts[slot] == 1 else f"{slot}{counts[slot]}"
            params[key] = val
            if not keep:
                text = text[:m.start(1)] + "{" + key + "}" + text[m.end(1):]
    return text.strip(), params

def _sub(val, params):
    """Recursively substitute {slot} placeholders in strings, dicts, and lists."""
    if isinstance(val, str):
        for slot, rep in params.items():
            val = val.replace("{" + slot + "}", rep)
        return val
    elif isinstance(val, dict):
        return {k: _sub(v, params) for k, v in val.items()}
    elif isinstance(val, list):
        return [_sub(v, params) for v in val]
    return val

def hydrate(steps, params):
    """Replace {slot} placeholders in steps with actual param values."""
    if not params:
        return steps
    return [_sub(step, params) if isinstance(step, dict) else step for step in steps]

# ── Similarity Engine ─────────────────────────────────────────────────────────

STOPWORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "is","it","my","me","i","do","can","please","just","up","out","into","how"
}

SYNONYMS = {
    "enable":"turn on",    "disable":"turn off",   "activate":"turn on",
    "deactivate":"turn off","toggle":"switch",
    "send":"message",      "msg":"message",         "dm":"message",     "text":"message",
    "set":"change",        "update":"change",       "modify":"change",  "edit":"change",
    "open":"launch",       "start":"launch",        "run":"launch",     "close":"stop",
    "show":"display",      "view":"display",        "see":"display",
    "photo":"picture",     "pic":"picture",         "image":"picture",
    "capture":"take",      "grab":"take",           "snap":"take",      "screenshot":"picture",
    "wifi":"wireless",     "wi-fi":"wireless",      "bluetooth":"bt",
    "wallpaper":"background","bg":"background",
    "volume":"sound",      "brightness":"screen",
    "wa":"whatsapp",       "ig":"instagram",        "fb":"facebook",
    "prefs":"settings",    "preferences":"settings",
    "call":"phone",        "ring":"phone",          "dial":"phone",
}

def normalize(token):
    return SYNONYMS.get(token, token).split()

def tokenize(text):
    text = re.sub(r'["\'][^"\']*["\']', '', text)
    text = re.sub(r'\b(?:saying|with message|with text)\s+.+$', '', text, flags=re.I)
    tokens = []
    for t in re.findall(r"[a-zA-Z0-9]+", text.lower()):
        if t not in STOPWORDS and len(t) > 1:
            tokens.extend(normalize(t))
    return tokens

def tfidf_vector(text):
    tokens = tokenize(text)
    counts = Counter(tokens)
    total = sum(counts.values()) or 1
    return {t: (c/total) * (1.0 + math.log(1 + 1/(1+c))) for t, c in counts.items()}

def cosine(v1, v2):
    if not v1 or not v2:
        return 0.0
    keys = set(v1) & set(v2)
    if not keys:
        return 0.0
    dot  = sum(v1[k]*v2[k] for k in keys)
    mag1 = math.sqrt(sum(x**2 for x in v1.values()))
    mag2 = math.sqrt(sum(x**2 for x in v2.values()))
    return 0.0 if (mag1 == 0 or mag2 == 0) else dot/(mag1*mag2)

def plan_id(template):
    return hashlib.sha1(template.lower().strip().encode()).hexdigest()[:16]

# ── Cache Search / Storage ────────────────────────────────────────────────────

def _plan_cols(conn):
    return [d[0] for d in conn.execute("SELECT * FROM plans LIMIT 0").description]


def seed_templates(conn):
    """Load built-in templates from templates.json into the plans table.

    This lets us ship known-good flows (like Facebook posting) without
    requiring every user to "teach" them via the LLM the first time.
    """
    tmpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json")
    if not os.path.exists(tmpl_path):
        return
    try:
        with open(tmpl_path, "r", encoding="utf-8") as f:
            templates = json.load(f)
    except Exception:
        return
    now = datetime.now().isoformat()
    for t in templates or []:
        template = t.get("template")
        steps = t.get("steps")
        param_slots = t.get("param_slots", [])
        context = t.get("context")
        if not template or not steps:
            continue
        pid = plan_id(template)
        row = conn.execute("SELECT id FROM plans WHERE id=?", (pid,)).fetchone()
        if row:
            continue
        vec = tfidf_vector(template)
        conn.execute(
            """INSERT INTO plans (id,template,intent_vec,steps,param_slots,context,
                   trusted,confirm_count,hits,failures,consecutive_failures,
                   created_at,last_used,last_result)
                VALUES (?,?,?,?,?,?,1,?,0,0,0,?,?,?)""",
            (pid, template, json.dumps(vec), json.dumps(steps), json.dumps(param_slots), context,
             N_CONFIRM, now, now, "seeded")
        )
    conn.commit()

def search_cache(conn, intent, threshold=THRESHOLD):
    template, _ = extract_params(intent)
    query_vec = tfidf_vector(template)
    rows = conn.execute(
        "SELECT * FROM plans WHERE consecutive_failures < ? ORDER BY last_used DESC",
        (MAX_FAILURES,)
    ).fetchall()
    if not rows:
        return None, 0.0

    cols = _plan_cols(conn)
    best, best_sim = None, 0.0

    # Extract lightweight verb set from the current intent so we can avoid
    # reusing a simple "open {app}" plan for a richer intent like
    # "open settings and scroll down".
    intent_tokens = set(tokenize(template))
    EXTRA_ACTION_WORDS = {"scroll", "search", "toggle", "enable", "disable", "message", "call"}

    for row in rows:
        p = dict(zip(cols, row))
        sim = cosine(query_vec, json.loads(p["intent_vec"]))

        if p["last_used"]:
            try:
                days = (datetime.now() - datetime.fromisoformat(p["last_used"])).days
                if days > DECAY_DAYS:
                    sim = max(0.0, sim - min(0.15, (days - DECAY_DAYS) * 0.005))
            except Exception:
                pass

        total = p["hits"] + p["failures"]
        if total > 0:
            sim *= (0.85 + 0.15 * (p["hits"] / total))

        # Penalize matches where the new intent introduces extra action
        # words (e.g. "scroll") that are not present in the stored
        # template. This prevents plain "open {app}" plans from being
        # reused for richer intents like "open {app} and scroll down".
        tmpl_tokens = set(tokenize(p["template"]))
        extra_verbs = {w for w in intent_tokens if w in EXTRA_ACTION_WORDS and w not in tmpl_tokens}
        if extra_verbs:
            sim *= 0.7

        if sim > best_sim:
            best_sim, best = sim, p

    if best and best_sim >= threshold:
        return best, best_sim
    return None, best_sim

def store_or_confirm(conn, intent, steps, param_slots=None, context=None):
    """Store new plan as PENDING or increment confirm_count. Returns (pid, newly_trusted)."""
    template, _ = extract_params(intent)
    pid = plan_id(template)
    vec = tfidf_vector(template)
    now = datetime.now().isoformat()
    slots = json.dumps(param_slots or [])

    existing = conn.execute("SELECT confirm_count, trusted FROM plans WHERE id=?", (pid,)).fetchone()
    if existing:
        new_count = existing[0] + 1
        newly_trusted = (not existing[1]) and (new_count >= N_CONFIRM)
        conn.execute("""
            UPDATE plans SET confirm_count=?, trusted=?,
                hits=hits+1, consecutive_failures=0,
                last_used=?, last_result='success', steps=?, param_slots=?
            WHERE id=?
        """, (new_count, 1 if (newly_trusted or existing[1]) else 0, now, json.dumps(steps), slots, pid))
    else:
        newly_trusted = N_CONFIRM <= 1
        conn.execute("""
            INSERT INTO plans (id,template,intent_vec,steps,param_slots,context,
                trusted,confirm_count,hits,failures,consecutive_failures,created_at,last_used,last_result)
            VALUES (?,?,?,?,?,?,?,1,1,0,0,?,?,?)
        """, (pid, template, json.dumps(vec), json.dumps(steps), slots, context,
              1 if newly_trusted else 0, now, now, "success"))
    conn.commit()
    return pid, newly_trusted

def mark_success(conn, pid):
    conn.execute("""
        UPDATE plans SET
            hits=hits+1, confirm_count=confirm_count+1,
            consecutive_failures=0,
            trusted=CASE WHEN confirm_count+1 >= ? THEN 1 ELSE trusted END,
            last_used=?, last_result='success'
        WHERE id=?
    """, (N_CONFIRM, datetime.now().isoformat(), pid))
    conn.commit()

def mark_failure(conn, pid, reason=""):
    conn.execute("""
        UPDATE plans SET failures=failures+1, consecutive_failures=consecutive_failures+1,
            last_used=?, last_result=? WHERE id=?
    """, (datetime.now().isoformat(), f"failure: {reason}"[:200], pid))
    conn.commit()
    row = conn.execute("SELECT consecutive_failures, template FROM plans WHERE id=?", (pid,)).fetchone()
    if row and row[0] >= MAX_FAILURES:
        print(f"⚠️  '{row[1]}' failed {row[0]}x, evicting")
        conn.execute("DELETE FROM plans WHERE id=?", (pid,))
        conn.commit()

def log_step(conn, pid, num, action, target, success, screen_changed, error, ms):
    conn.execute("""
        INSERT INTO step_log (ts,plan_id,step_num,action,target,success,screen_changed,error,duration_ms)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (datetime.now().isoformat(), pid, num, action, str(target)[:200],
          success, screen_changed, str(error or "")[:200], ms))
    conn.commit()

def log_task(conn, intent, template, source, pid, sim, success, tokens, ms, steps):
    conn.execute("""
        INSERT INTO task_log (ts,intent,template,source,plan_id,similarity,success,tokens_used,duration_ms,steps_taken)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now().isoformat(), intent, template, source, pid, sim, success, tokens, ms, steps))
    conn.commit()

# ── App Cache ─────────────────────────────────────────────────────────────────

KNOWN_APPS = {
    "com.whatsapp":"whatsapp", "com.instagram.android":"instagram",
    "com.facebook.katana":"facebook", "com.twitter.android":"twitter",
    "com.google.android.youtube":"youtube", "com.google.android.gm":"gmail",
    "com.google.android.apps.maps":"maps", "com.android.settings":"settings",
    "com.android.chrome":"chrome", "com.spotify.music":"spotify",
    "com.snapchat.android":"snapchat", "com.netflix.mediaclient":"netflix",
    "com.discord":"discord", "com.reddit.frontpage":"reddit",
    "com.google.android.dialer":"phone", "com.google.android.contacts":"contacts",
    "com.samsung.android.dialer":"phone", "com.samsung.android.messaging":"messages",
}

def refresh_apps(conn):
    print("  Refreshing installed app list...")
    try:
        r = subprocess.run(
            f"adb -s localhost:{ADB_PORT} shell pm list packages",
            shell=True, capture_output=True, text=True, timeout=15
        )
        pkgs = [l.replace("package:", "").strip() for l in r.stdout.splitlines() if l.startswith("package:")]
        now = datetime.now().isoformat()
        for pkg in pkgs:
            name = KNOWN_APPS.get(pkg, pkg.split(".")[-1].lower())
            conn.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)", (pkg, name, now))
        conn.commit()
        print(f"  ✅ {len(pkgs)} apps cached")
    except Exception as e:
        print(f"  ⚠️  App refresh failed: {e}")

def resolve_app(conn, name):
    row = conn.execute(
        "SELECT package FROM app_cache WHERE name=? OR package=? LIMIT 1",
        (name.lower(), name.lower())
    ).fetchone()
    return row[0] if row else name

# ── Element Resolver ──────────────────────────────────────────────────────────

def resolve_element(elements, target):
    """Find best UI element for target spec (str or {text,id,desc,label} dict).

    Checks text, label, content-desc, and resource-id (hyphen and underscore
    variants) so mismatches between wrangle's field names don't cause misses.

    Also has a small special case for search: when targeting a generic
    "Search" field, ignore the "Voice search" mic button so we tap the
    text field instead.
    """
    if not elements or not target:
        return None
    if isinstance(target, str):
        target = {"text": target}

    # Detect "search"-ish targets (but not "voice search").
    t_text = target.get("text", "")
    t_desc = target.get("desc", "")
    tt = f"{t_text} {t_desc}".lower()
    wants_search = "search" in tt and "voice" not in tt

    candidates = []
    for el in elements:
        score = 0
        raw_label = (el.get("text", "") or el.get("label", "") or "")
        t  = raw_label.lower()
        d  = (el.get("content-desc","") or el.get("content_desc","") or "").lower()
        i  = (el.get("resource-id","")  or el.get("resource_id","")  or "").lower()

        if wants_search:
            # Never treat the voice-search mic as the search field.
            if "voice search" in t or "voice search" in d:
                continue

        # Local ChatGPT web input tuning (your phone):
        # - Strongly prefer the "Ask anything" textarea
        # - Never treat the "Add files and more" plus button as the input
        if "prompt-textarea" in i or "ask anything" in t:
            score += 25
        if "composer-plus-btn" in i or "add files and more" in t:
            score -= 20

        if "text" in target:
            needle = target["text"].lower()
            if needle in t:
                score += 10 + (10 if t == needle else 0)
        if "label" in target:
            needle = target["label"].lower()
            if needle in t:
                score += 10 + (10 if t == needle else 0)
        if "desc" in target:
            needle = target["desc"].lower()
            if needle in d:
                score += 8
        if "id" in target:
            needle = target["id"].lower()
            if needle in i:
                score += 12

        # Facebook composer tuning: when targeting the post text field,
        # strongly prefer the big multi-line text area in the composer and
        # avoid hitting other controls.
        if target.get("desc") == "Post text field":
            # Heuristic: large view in the middle of the composer with no
            # resource-id but big bounds is likely the text area.
            try:
                bounds = el.get("bounds", "")
                coords = bounds.replace("][", ",").replace("[", "").replace("]", "").split(",")
                x1, y1, x2, y2 = map(int, coords)
                h = y2 - y1
            except Exception:
                h = 0
            if h > 200 and 0.2 < el.get("y_norm", 0.5) < 0.8:
                score += 15
        if score > 0 and el.get("clickable"):
            score += 5
        if score > 0 and el.get("editable"):
            score += 3
        if score > 0 and el.get("focused"):
            score += 4
        if score > 0:
            candidates.append((score, el))

    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]

# ── Wrangle Bridge ───────────────────────────────────────────────────────────

def pc_get_state(task=""):
    try:
        r = subprocess.run([PYTHON, WRANGLE, "get_state", "--task", task],
                           capture_output=True, text=True, timeout=20)
        state = json.loads(r.stdout.strip())
        # state_signature: semantic fingerprint, more stable than screen_hash alone.
        # Uses fields wrangle actually returns: foreground_app, element labels, element ids.
        # "label" is the primary text field wrangle exports; falls back to "text" if present.
        if "elements" in state and "error" not in state:
            top_labels = []
            top_ids    = []
            for e in state["elements"][:6]:
                lbl = e.get("label") or e.get("text","") or e.get("content-desc","")
                if lbl: top_labels.append(lbl.strip()[:30])
                eid_raw = e.get("resource-id","") or e.get("id","")
                eid = str(eid_raw).split("/")[-1] if eid_raw is not None else ""
                if eid: top_ids.append(eid[:20])
            sig_raw = f"{state.get('foreground_app','')}|{'|'.join(top_labels[:4])}|{'|'.join(top_ids[:4])}"
            state["state_signature"] = hashlib.md5(sig_raw.encode()).hexdigest()[:12]
        else:
            state.setdefault("state_signature", "unknown")
        return state
    except Exception as e:
        return {"error": str(e), "elements": [], "screen_hash": "error",
                "foreground_app": "unknown", "state_signature": "error"}

def pc_do_action(action):
    """Call wrangle's do_action CLI and parse the JSON result.

    In some edge cases (especially for type actions) wrangle may complete
    successfully but emit an empty stdout or non-JSON noise. In those cases
    we treat the action as best-effort success instead of hard-failing the
    whole IMPRINT run.

    Note: `type` actions do more work inside wrangle (IME switch + UI dump),
    so we give them a longer timeout than other actions to avoid spurious
    failures on slower devices.
    """
    try:
        timeout = 60 if (action or {}).get("action") == "type" else 20
        r = subprocess.run([PYTHON, WRANGLE, "do_action", "--json", json.dumps(action)],
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        if out:
            try:
                return json.loads(out)
            except Exception as e:
                # For type actions, assume success if the subprocess ran.
                if action.get("action") == "type" and r.returncode == 0:
                    return {"ok": True, "executed": "type", "text": action.get("text", "")}
                return {"ok": False, "error": f"invalid json from wrangle: {e}"}
        # No output at all: treat type as fire-and-forget success.
        if action.get("action") == "type" and r.returncode == 0:
            return {"ok": True, "executed": "type", "text": action.get("text", "")}
        return {"ok": False, "error": "empty response from wrangle"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def keyboard_open():
    """Detect if soft keyboard is visible. Works across Android versions."""
    try:
        r = subprocess.run(
            f"adb -s localhost:{ADB_PORT} shell dumpsys input_method",
            shell=True, capture_output=True, text=True, timeout=8
        )
        out = r.stdout
        # Android < 11: mInputShown=true
        # Android 11+:  mIsInputViewShown=true  or  isInputShown=true
        return (
            "mInputShown=true" in out
            or "mIsInputViewShown=true" in out
            or "isInputShown=true" in out
        )
    except Exception:
        return False

def input_focused(elements):
    return any(
        el.get("focused") and "EditText" in el.get("class","")
        for el in elements
    )

# ── Step Execution ────────────────────────────────────────────────────────────

def execute_one_step(step, elements, intent="", hash_before=""):
    """Run one step. Returns structured result dict.

    hash_before: pass the screen hash already known to the caller to avoid
                  a redundant pc_get_state() call on every step.
    Returns dict with optional 'state_after' key when screen changed, so the
    caller can reuse it without another UIAutomator dump.
    """
    action = step.get("action", "")
    t0 = time.time()

    target_spec = step.get("target")
    resolved_el = resolve_element(elements, target_spec) if target_spec else None

    result = {
        "action": action, "target": str(target_spec or step.get("reason","")),
        "success": False, "screen_changed": False,
        "error": None, "hash_before": hash_before, "hash_after": hash_before, "duration_ms": 0,
    }

    payload = dict(step)
    if resolved_el:
        payload["x"] = resolved_el.get("x", step.get("x", 540))
        payload["y"] = resolved_el.get("y", step.get("y", 1170))
        log.debug(f"Resolved '{target_spec}' → ({payload['x']},{payload['y']}) text='{resolved_el.get('text','')}'")
    elif target_spec and action == "tap":
        # Resolver couldn't find a matching element; fall back to safe coords
        # instead of passing no x/y and blowing up inside wrangle.
        t_str = str(target_spec)
        # Facebook "Create post" heuristic: tap the feed composer row, not
        # the people/music row. Empirically at ~y=680 on this device.
        if "Create post" in t_str or "What's on your mind" in t_str:
            cx, cy = 540, 681
        else:
            cx = step.get("x") or (elements[0]["x"] if elements else 540)
            cy = step.get("y") or (elements[0]["y"] if elements else 1170)
        payload["x"], payload["y"] = int(cx), int(cy)
        log.debug(
            f"Target '{target_spec}' not found in {len(elements)} elements, "
            f"using fallback coords ({payload['x']},{payload['y']})"
        )

    # Special-case: on ChatGPT web, pressing Enter adds a newline instead of
    # sending. If this step is a KEYCODE_ENTER and we see a visible "Send"
    # button (composer-submit-button), convert the action into a tap on that
    # button instead of a keyevent.
    if action == "keyevent" and step.get("key") == "KEYCODE_ENTER":
        send_el = None
        for el in elements:
            rid = (el.get("resource-id") or el.get("resource_id") or "").lower()
            lbl = (el.get("text") or el.get("label") or "").lower()
            if "composer-submit-button" in rid or "send prompt" in lbl or lbl == "send":
                send_el = el
                break
        if send_el:
            payload = {"action": "tap", "x": int(send_el["x"]), "y": int(send_el["y"])}
            action = "tap"
            log.debug(
                f"Converted Enter keyevent into tap on send button at "
                f"({payload['x']},{payload['y']}) label='{send_el.get('text','')}'"
            )

    if action == "type" and not input_focused(elements) and not keyboard_open():
        log.debug("No focused input before type, proceeding anyway")

    # Facebook composer special-case: if this is the "publish" tap for a
    # post, many builds require tapping a "Done" control before tapping
    # the final "Post" button. On your device the labels can vary slightly,
    # so we treat anything with text/id containing "done" as Done, and
    # anything with text/id containing "post" as Post.
    if action == "tap" and "post" in str(step.get("target", "")).lower():
        try:
            done_el, post_el = None, None
            for el in elements:
                txt = (el.get("text") or el.get("label") or "").strip().lower()
                rid = (el.get("resource-id") or el.get("resource_id") or "").lower()
                x_n = float(el.get("x_norm", 0.5) or 0.5)
                y_n = float(el.get("y_norm", 0.5) or 0.5)
                # Prefer top-right clickable as Done
                if ("done" in txt or "done" in rid) and el.get("clickable") and x_n > 0.6 and y_n < 0.25:
                    done_el = el
                # Prefer bottom-right clickable as Post
                if ("post" in txt or "post" in rid) and el.get("clickable") and x_n > 0.6 and y_n > 0.6:
                    post_el = el
            if done_el and post_el:
                pc_do_action({"action": "tap", "x": int(done_el["x"]), "y": int(done_el["y"])} )
                time.sleep(0.4)
                pc_do_action({"action": "tap", "x": int(post_el["x"]), "y": int(post_el["y"])} )
                result["success"] = True
                result["screen_changed"] = True
                result["duration_ms"] = int((time.time() - t0) * 1000)
                return result
        except Exception:
            pass

    # ChatGPT app special-case: when we intend to tap the send control but the
    # LLM only described it ("Send button at the bottom right" / "send control"),
    # pick the most bottom-right clickable element rather than tapping inside
    # the prompt textbox.
    if (action == "tap" and
        ("send" in (step.get("reason", "").lower()) or "send control" in str(step.get("target", "")).lower())):
        try:
            best = None
            best_score = -1.0
            for el in elements:
                if not el.get("clickable"):
                    continue
                x_n = float(el.get("x_norm", 0.5) or 0.5)
                y_n = float(el.get("y_norm", 0.5) or 0.5)
                # Encourage mid-right; send icon on your device sits around the middle-right edge
                score = (x_n * 0.7) + (y_n * 0.3)
                if 0.4 < y_n < 0.8 and x_n > 0.8 and score > best_score:
                    best = el
                    best_score = score
            if best is not None:
                payload["x"], payload["y"] = int(best["x"]), int(best["y"])
        except Exception:
            pass

    exec_r = pc_do_action(payload)
    result["duration_ms"] = int((time.time() - t0) * 1000)

    if not exec_r.get("ok"):
        result["error"] = exec_r.get("error", "unknown")
        return result

    # Chrome-specific fallback: sometimes KEYCODE_ENTER in the address bar
    # does not trigger navigation even though text is present. If this was
    # an Enter keyevent, the foreground app is Chrome, and the screen hash
    # did not change, fall back to opening a Google search URL for the
    # apparent query instead of leaving the text inert in the bar.
    if (action == "keyevent" and step.get("key") == "KEYCODE_ENTER" and
        hash_before and phone_state.get("foreground_app","")):
        fg = phone_state["foreground_app"]
        if "com.android.chrome" in fg or "chrome" in fg.lower():
            # Heuristic: pull query from the original intent.
            q = ""
            intent_l = (intent or "").strip()
            m = re.search(r"(?:search for|type)\s+(.+)$", intent_l, flags=re.I)
            if m:
                q = m.group(1).strip()
            else:
                parts = intent_l.split()
                if len(parts) >= 2:
                    q = " ".join(parts[-2:])
            if q:
                url = "https://www.google.com/search?q=" + urllib.parse.quote(q)
                pc_do_action({"action": "open_url", "url": url})
                result["screen_changed"] = True
                result["hash_after"] = "chrome_search_fallback"
                return result

    DEFAULT_DELAYS = {
        "launch":   2.5,
        "open_url": 2.5,
        "tap":      1.0,
        "swipe":    0.6,
        "scroll":   0.6,
        "keyevent": 0.5,
        "back":     0.8,
        "type":     0.4,
    }
    default_delay = DEFAULT_DELAYS.get(action, 1.0)
    time.sleep(step.get("delay", default_delay))

    if action in ("tap","swipe","scroll","launch","open_url","back","keyevent","type"):
        state_after = pc_get_state(task=intent)
        hash_after  = state_after.get("screen_hash", "")
        result["hash_after"]     = hash_after
        result["screen_changed"] = bool(hash_after and hash_after != hash_before and hash_after != "error")
        if result["screen_changed"]:
            # Return the fresh state so the caller can reuse it without another dump
            result["state_after"] = state_after
        if action == "tap" and not result["screen_changed"]:
            log.debug(f"Tap at step: screen unchanged (may be normal)")

    result["success"] = True
    return result

DESTRUCTIVE_ACTIONS = {
    "delete", "remove", "uninstall", "reset", "clear", "format",
    "purchase", "buy", "pay", "checkout",
    "send",   # confirmed in context: message sending is OK, but flag for aware execution
    "submit", "post", "publish", "share",
    "call",   # actually dials a number
}

DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(delete|remove|uninstall|factory reset|wipe|purchase|buy|pay|submit|post|publish)\b",
    re.I
)

def is_destructive(intent):
    """Return True if the intent looks like it could cause irreversible side effects."""
    return bool(DESTRUCTIVE_PATTERNS.search(intent))

def execute_steps(conn, steps, intent="", plan_id_str=None, params=None,
                  task_deadline=None, confirmed=False, interactive=True):
    """
    Execute steps with retry, mid-task replan, timeout, and max-steps guard.

    Also applies small, intent-aware post-processing tweaks, e.g. for
    search-like tasks that type into a field but forget to submit.

    confirmed=True   : skip destructive safety prompt (OpenClaw dispatcher mode)
    interactive=True : prompt human via stdin if not confirmed (default for CLI use)
                       set False when called from dispatch context to fail-safe instead

    Returns (success, drift, error, steps_taken, step_results)
    """
    if params:
        steps = hydrate(steps, params)

    # Auto-add a submit step for search-like intents that only type text
    # but never submit it. This keeps simple "search for X" flows usable
    # even when the planner forgets an Enter key.
    intent_l = (intent or "").lower()
    if ("search" in intent_l or "look up" in intent_l or
        "address bar" in intent_l or "search bar" in intent_l):
        has_type = any((s.get("action") == "type") for s in steps)
        has_submit = any(s.get("action") in ("keyevent", "open_url") for s in steps)
        if has_type and not has_submit:
            steps = list(steps) + [{
                "action": "keyevent",
                "key": "KEYCODE_ENTER",
                "reason": "submit search",
                "delay": 0.5,
            }]

    # Safety gate: block or prompt on destructive intents
    if is_destructive(intent) and not confirmed:
        print(f"  ⚠️  SAFETY: '{intent}' matches destructive action pattern.")
        if interactive:
            try:
                ans = input("  Proceed? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans != "y":
                return False, False, ERR_UNSAFE_BLOCKED, 0, []
        else:
            # Non-interactive (dispatcher): fail safe, require explicit confirmed=True
            return False, False, ERR_UNSAFE_BLOCKED, 0, []

    step_results = []
    steps_taken  = 0
    consecutive_step_failures = 0
    deadline     = task_deadline or (time.time() + TASK_TIMEOUT)

    print(f"  ⚡ Executing {len(steps)} steps...")

    state    = pc_get_state(task=intent)
    elements = state.get("elements", [])
    init_hash = state.get("screen_hash","")
    current_hash = init_hash

    # Drift check
    first_hash = steps[0].get("screen_hash_before") if steps else None
    if first_hash and init_hash and init_hash != first_hash:
        print(f"  ⚠️  Drift (expected {first_hash[:8]}, got {init_hash[:8]})")
        return False, True, ERR_SCREEN_DRIFT, 0, []

    for i, step in enumerate(steps):
        if step.get("action") == "done":
            break
        if steps_taken >= MAX_STEPS:
            return False, False, ERR_MAX_STEPS, steps_taken, step_results
        if time.time() > deadline:
            return False, False, ERR_TIMEOUT, steps_taken, step_results

        print(f"  [{i+1}/{len(steps)}] {step.get('action')}, {step.get('reason','')}")

        step_ok, last_err = False, None
        for attempt in range(1, MAX_RETRIES + 2):
            if attempt > 1:
                print(f"    ↺ retry {attempt-1}/{MAX_RETRIES}...")
                time.sleep(1.0)
                state    = pc_get_state(task=intent)
                elements = state.get("elements", [])
                current_hash = state.get("screen_hash", current_hash)

            # Pass current hash so execute_one_step doesn't need to fetch state again
            sr = execute_one_step(step, elements, intent=intent, hash_before=current_hash)
            step_results.append(sr)

            if plan_id_str:
                log_step(conn, plan_id_str, i+1, sr["action"], sr["target"],
                         sr["success"], sr["screen_changed"], sr["error"], sr["duration_ms"])

            if sr["success"]:
                step_ok = True
                # Reuse state_after returned by execute_one_step if available
                if sr.get("state_after"):
                    state    = sr["state_after"]
                    elements = state.get("elements", [])
                    current_hash = state.get("screen_hash", sr["hash_after"])
                elif sr["screen_changed"]:
                    state    = pc_get_state(task=intent)
                    elements = state.get("elements", [])
                    current_hash = state.get("screen_hash", current_hash)
                else:
                    current_hash = sr.get("hash_after", current_hash)
                break
            last_err = sr["error"]

        # steps_taken counts logical steps, not retry attempts
        steps_taken += 1

        if not step_ok:
            consecutive_step_failures += 1
            print(f"  ❌ Step {i+1} failed after {MAX_RETRIES} retries: {last_err}")

            # Mid-task replan: only fire after 2 consecutive step failures to avoid
            # burning tokens on transient issues (slow load, mid-transition screen, etc.)
            if consecutive_step_failures >= 2:
                remaining = [s for s in steps[i:] if s.get("action") != "done"]
                if remaining and OPENCLAW_SESSION:
                    print(f"  🔄 Mid-task replan ({consecutive_step_failures} consecutive failures)...")
                    replan_t0 = time.time()
                    cur_state = state  # reuse state we already have
                    revised, r_tok, r_ms = ask_llm_replan(intent, step, cur_state, remaining)
                    if revised:
                        print(f"  ↺ Replan: {len(revised)} revised steps")
                        r_suc, _, r_err, r_steps, r_results = execute_steps(
                            conn, revised, intent=intent,
                            plan_id_str=plan_id_str, task_deadline=deadline
                        )
                        step_results.extend(r_results)
                        steps_taken += r_steps
                        if r_suc:
                            replan_ms = int((time.time() - replan_t0) * 1000)
                            log_task(conn, intent, "", "replan", plan_id_str, 0.0, 1,
                                     r_tok, replan_ms, steps_taken)
                            return True, False, None, steps_taken, step_results
                        last_err = r_err
            else:
                print(f"  ⏭  Skipping replan (need 2 consecutive failures, have {consecutive_step_failures})")

            return False, False, f"step {i+1} ({step.get('action')}): {last_err}", steps_taken, step_results
        else:
            consecutive_step_failures = 0  # reset on success

    return True, False, None, steps_taken, step_results

# ── LLM Planning ─────────────────────────────────────────────────────────────

PLAN_SYSTEM = """You are a planning engine for an Android phone automation agent.
Decompose tasks into minimal abstract parameterized steps.
RULES:
- Use {slot} placeholders for variable content (contacts, messages, search queries, etc.)
- Prefer element targeting via text/id/desc over raw x/y coordinates
- Valid actions: launch, open_url, tap, type, swipe, scroll, back, keyevent, done
- Every step needs: action, reason, delay (float seconds)
- Always end with {"action":"done","reason":"..."}
- For web searches or specific URLs, prefer open_url over launch+tap+type
- Return ONLY a JSON array. No markdown. No explanation.

Action reference:
  launch:   {"action":"launch","app":"youtube","reason":"...","delay":2.5}
  open_url: {"action":"open_url","url":"https://...","reason":"...","delay":2.5}
  tap:      {"action":"tap","target":{"text":"Search"},"reason":"...","delay":1.0}
  type:     {"action":"type","text":"{query}","reason":"...","delay":0.5}
  keyevent: {"action":"keyevent","key":"KEYCODE_ENTER","reason":"...","delay":0.5}
  scroll:   {"action":"scroll","direction":"down","amount":800,"reason":"...","delay":0.6}
  swipe:    {"action":"swipe","x1":540,"y1":1400,"x2":540,"y2":600,"ms":300,"reason":"...","delay":0.6}
  back:     {"action":"back","reason":"...","delay":0.8}"""

def _ask_openclaw(prompt, timeout=60):
    """
    Send a prompt to OpenClaw via CLI and return (text, tokens, duration_ms).
    Uses: openclaw agent --session-id <session> --message <prompt> --json
    Parses: result.payloads[0].text and result.meta.agentMeta.usage.total
    """
    t0 = time.time()
    try:
        r = subprocess.run(
            ["openclaw", "agent", "--session-id", OPENCLAW_SESSION,
             "--message", prompt, "--json"],
            capture_output=True, text=True, timeout=timeout
        )
        ms = int((time.time() - t0) * 1000)
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip()
            return None, f"openclaw exit {r.returncode}: {err[:200]}", 0, ms
        data = json.loads(r.stdout.strip())
        text = data["result"]["payloads"][0]["text"].strip()
        tok  = data["result"]["meta"]["agentMeta"]["usage"].get("total", 0)
        return text, None, tok, ms
    except subprocess.TimeoutExpired:
        return None, "openclaw timeout", 0, int((time.time()-t0)*1000)
    except Exception as e:
        return None, str(e), 0, int((time.time()-t0)*1000)

def ask_llm_for_plan(intent, template, params, phone_state):
    elements = []
    for el in phone_state.get("elements", [])[:20]:
        e = {}
        if el.get("text"):         e["text"] = el["text"]
        if el.get("resource-id"):  e["id"]   = el["resource-id"].split("/")[-1]
        if el.get("content-desc"): e["desc"] = el["content-desc"]
        if el.get("clickable"):    e["click"] = True
        if el.get("x"):            e["x"], e["y"] = el["x"], el["y"]
        if e: elements.append(e)

    prompt = f"""{PLAN_SYSTEM}

Task: {intent}
Template: {template}
Params: {json.dumps(params)}
Foreground: {phone_state.get("foreground_app","unknown")}
UI elements: {json.dumps(elements)}

Plan abstract steps using {{slot}} placeholders. Prefer element targeting.
Example target: {{"text":"Send"}}, {{"id":"send_button"}}, {{"desc":"Search"}}"""

    raw, error, tok, ms = _ask_openclaw(prompt, timeout=60)
    if error:
        return None, error, tok, ms
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if not m:
        return None, f"No JSON array in response: {raw[:200]}", tok, ms
    try:
        return json.loads(m.group(0)), None, tok, ms
    except Exception as e:
        return None, f"JSON parse error: {e}", tok, ms

def ask_llm_replan(intent, failed_step, current_state, remaining_steps):
    # Use same compact format as ask_llm_for_plan; raw elements are ~20 fields each
    compact_elements = []
    for el in current_state.get("elements", [])[:15]:
        e = {}
        if el.get("text") or el.get("label"):
            e["text"] = el.get("text") or el.get("label")
        rid = el.get("resource-id") or el.get("resource_id","")
        if rid: e["id"] = rid.split("/")[-1]
        desc = el.get("content-desc") or el.get("content_desc","")
        if desc: e["desc"] = desc
        if el.get("clickable"): e["click"] = True
        if el.get("editable"):  e["edit"]  = True
        if el.get("focused"):   e["focused"] = True
        if el.get("x"):         e["x"], e["y"] = el["x"], el["y"]
        if e: compact_elements.append(e)

    prompt = f"""{PLAN_SYSTEM}

Task: {intent}
Failed step: {json.dumps(failed_step)}
Current app: {current_state.get("foreground_app","unknown")}
Current UI: {json.dumps(compact_elements)}
Remaining planned: {json.dumps(remaining_steps)}

Provide ONLY revised remaining steps as JSON array to complete the task from current screen."""

    raw, error, tok, ms = _ask_openclaw(prompt, timeout=45)
    if error or not raw:
        return None, tok, ms
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    return (json.loads(m.group(0)) if m else None), tok, ms

# ── Core Router ───────────────────────────────────────────────────────────────

def route(intent, conn, dry_run=False, confirmed=False):
    t0       = time.time()
    deadline = t0 + TASK_TIMEOUT
    template, params = extract_params(intent)

    # Refresh app cache if stale (>24h) or empty
    app_count = conn.execute("SELECT COUNT(*) FROM app_cache").fetchone()[0]
    last_refresh = conn.execute(
        "SELECT MAX(updated_at) FROM app_cache"
    ).fetchone()[0]
    if app_count == 0 or (last_refresh and
            (datetime.now() - datetime.fromisoformat(last_refresh)).total_seconds() > 86400):
        log.debug("App cache stale, refreshing...")
        refresh_apps(conn)

    print(f"\n{'='*54}")
    print(f"  IMPRINT v{VERSION}")
    print(f"  Intent:   {intent}")
    print(f"  Template: {template}")
    if params: print(f"  Params:   {params}")
    print(f"{'='*54}")

    plan, sim = search_cache(conn, intent)

    # If this intent specifies an app (via {app}) and the stored plan has a
    # different foreground app context (e.g. settings vs chrome), treat it
    # as a cache miss so we don't reuse a "scroll in settings" plan for
    # "open chrome and scroll".
    if plan and params.get("app") and plan.get("context"):
        try:
            target_pkg = resolve_app(conn, params["app"])
            plan_ctx   = str(plan["context"]).strip()
            if plan_ctx and target_pkg and plan_ctx != target_pkg:
                print(f"\n⚠️  Cache plan context '{plan_ctx}' != target app '{target_pkg}', ignoring plan")
                plan, sim = None, 0.0
        except Exception:
            pass

    if plan:
        trusted = bool(plan["trusted"])
        status  = "TRUSTED" if trusted else f"PENDING {plan['confirm_count']}/{N_CONFIRM}"
        print(f"\n✅ Cache HIT [{status}] sim={sim:.2f}  '{plan['template']}'")
        print(f"   {plan['hits']}✓ {plan['failures']}✗")

        steps = json.loads(plan["steps"])

        if dry_run:
            hydrated = hydrate(steps, params)
            print(f"\n[DRY RUN] {len(steps)} steps:")
            for i, s in enumerate(hydrated):
                tgt = s.get("target") or s.get("reason","")
                print(f"  [{i+1}] {s.get('action')} → {tgt}")
            return {"source":"cache","plan_id":plan["id"],"similarity":sim,"dry_run":True}

        success, drift, error, steps_taken, _ = execute_steps(
            conn, steps, intent=intent, plan_id_str=plan["id"],
            params=params, task_deadline=deadline
        , confirmed=confirmed)
        ms = int((time.time()-t0)*1000)

        if drift:
            print(f"\n⚠️  Drift, falling through to LLM...")
            mark_failure(conn, plan["id"], "drift")
            plan = None

        elif success:
            mark_success(conn, plan["id"])
            new_count = plan["confirm_count"] + 1
            if not trusted and new_count >= N_CONFIRM:
                print(f"  🎯 Plan PROMOTED to TRUSTED after {N_CONFIRM} confirmations!")
            log_task(conn, intent, template, "cache", plan["id"], sim, 1, 0, ms, steps_taken)
            print(f"\n✅ DONE  cache · 0 tokens · {ms}ms · {steps_taken} steps")
            return {"source":"cache","plan_id":plan["id"],"similarity":sim,
                    "tokens":0,"duration_ms":ms,"steps_taken":steps_taken}

        else:
            mark_failure(conn, plan["id"], error)
            log_task(conn, intent, template, "cache", plan["id"], sim, 0, 0, ms, steps_taken)
            print(f"\n❌ Failed: {error}")
            return {"source":"cache","plan_id":plan["id"],"success":False,"error":error}

    # ── LLM path ──────────────────────────────────────────────────────────────
    if not plan:
        print(f"\n🔍 Cache MISS (best sim={sim:.2f}), querying LLM...")

        if not OPENCLAW_SESSION:
            return {"source":"llm","success":False,"error":ERR_NO_LLM_KEY}

        print("  Getting phone state...")
        phone_state = pc_get_state(task=intent)
        if phone_state.get("error"):
            print(f"  ⚠️  {phone_state['error']}, planning blind")
            phone_state = {"elements":[],"foreground_app":"unknown","screen_hash":""}

        steps, error, tokens, llm_ms = ask_llm_for_plan(intent, template, params, phone_state)

        if error or not steps:
            print(f"❌ LLM failed: {error}")
            log_task(conn, intent, template, "llm", None, 0.0, 0, tokens, llm_ms, 0)
            return {"source":"llm","success":False,"error":error}

        print(f"  LLM: {len(steps)} steps · {tokens} tokens · {llm_ms}ms")

        if dry_run:
            hydrated = hydrate(steps, params)
            print(f"\n[DRY RUN] {len(steps)} LLM steps:")
            for i, s in enumerate(hydrated):
                print(f"  [{i+1}] {s.get('action')} → {s.get('target') or s.get('reason','')}")
            return {"source":"llm","steps":steps,"tokens":tokens,"dry_run":True}

        success, drift, error, steps_taken, _ = execute_steps(
            conn, steps, intent=intent, params=params, task_deadline=deadline
        , confirmed=confirmed)
        ms = int((time.time()-t0)*1000)

        if success:
            # Use the logical app parameter as context when available so
            # app-specific plans (e.g. "open settings and scroll") are not
            # reused for different apps (e.g. "open chrome and scroll").
            ctx = params.get("app") or phone_state.get("foreground_app")
            pid, newly_trusted = store_or_confirm(
                conn, intent, steps,
                param_slots=list(params.keys()),
                context=ctx
            )
            log_task(conn, intent, template, "llm", pid, 0.0, 1, tokens, ms, steps_taken)
            trust_msg = "TRUSTED" if newly_trusted else f"PENDING (1/{N_CONFIRM})"
            print(f"\n✅ DONE  LLM · {tokens} tokens · {ms}ms")
            print(f"   Stored '{pid}' as {trust_msg}, next time: {'0 tokens' if newly_trusted else f'need {N_CONFIRM-1} more run(s)'}")
            return {"source":"llm","plan_id":pid,"tokens":tokens,"duration_ms":ms,
                    "steps_taken":steps_taken,"trusted":newly_trusted}
        else:
            log_task(conn, intent, template, "llm", None, 0.0, 0, tokens, ms, steps_taken)
            print(f"\n❌ LLM plan failed: {error}")
            return {"source":"llm","success":False,"error":error,"tokens":tokens}

# ── Offline Queue ────────────────────────────────────────────────────────────

def queue_task(conn, intent, dry_run=False):
    """
    Queue a task for later execution (e.g. when ADB is unavailable).
    Useful when phone is offline / ADB unreachable temporarily.
    """
    conn.execute(
        "INSERT INTO queue (ts, intent, dry_run, status) VALUES (?,?,?,'pending')",
        (datetime.now().isoformat(), intent, 1 if dry_run else 0)
    )
    conn.commit()
    print(f"📥 Queued: '{intent}' (will run when ADB is available)")

def flush_queue(conn):
    """
    Execute all pending queued tasks.
    Call this on reconnect or at startup to drain backlog.
    """
    rows = conn.execute(
        "SELECT id, intent, dry_run FROM queue WHERE status='pending' ORDER BY id ASC"
    ).fetchall()
    if not rows:
        print("Queue empty.")
        return

    print(f"📤 Flushing {len(rows)} queued task(s)...")
    for row_id, intent, dry_run in rows:
        print(f"  ▶ '{intent}'")
        try:
            result = route(intent, conn, dry_run=bool(dry_run))
            success = result.get("success", True)  # absence of 'success'=False means ok
            conn.execute("""
                UPDATE queue SET status=?, result=?, processed_at=? WHERE id=?
            """, ('done' if success else 'failed',
                  json.dumps(result)[:500], datetime.now().isoformat(), row_id))
        except Exception as e:
            conn.execute("UPDATE queue SET status='error', error=? WHERE id=?",
                         (str(e)[:200], row_id))
        conn.commit()
    print("✅ Queue flushed.")

def show_queue(conn):
    rows = conn.execute(
        "SELECT id, ts, intent, status FROM queue ORDER BY id DESC LIMIT 20"
    ).fetchall()
    if not rows:
        print("Queue is empty.")
        return
    print(f"\n── Queue ({'─'*42})")
    for r in rows:
        icon = {"pending":"⏳","done":"✅","failed":"❌","error":"💥"}.get(r[3],"?")
        print(f"  {icon} [{r[0]}] {r[2][:50]:<50} {r[3]}  {r[1][:16]}")
    print()

# ── Stats & Management ────────────────────────────────────────────────────────

def print_stats(conn):
    plans   = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    trusted = conn.execute("SELECT COUNT(*) FROM plans WHERE trusted=1").fetchone()[0]
    total   = conn.execute("SELECT COUNT(*) FROM task_log").fetchone()[0]
    hits    = conn.execute("SELECT COUNT(*) FROM task_log WHERE source='cache'").fetchone()[0]
    llm_n   = conn.execute("SELECT COUNT(*) FROM task_log WHERE source='llm'").fetchone()[0]
    replans = conn.execute("SELECT COUNT(*) FROM task_log WHERE source='replan'").fetchone()[0]
    tokens  = conn.execute("SELECT SUM(tokens_used) FROM task_log").fetchone()[0] or 0
    pct     = (hits/total*100) if total else 0

    print(f"\n── IMPRINT v{VERSION} Stats ─────────────────────────────")
    print(f"  Plans:          {plans} ({trusted} trusted, {plans-trusted} pending)")
    print(f"  Tasks run:      {total}")
    print(f"  Cache hits:     {hits} ({pct:.1f}%)")
    print(f"  LLM calls:      {llm_n}")
    print(f"  Mid replans:    {replans}")
    print(f"  Tokens spent:   {tokens:,}")
    print(f"  Est. saved:     {hits*800:,}  (@~800/plan)")
    print(f"  DB:             {DB_PATH}")
    print()

def list_plans(conn):
    rows = conn.execute(
        "SELECT template,trusted,confirm_count,hits,failures,last_used,param_slots FROM plans ORDER BY hits DESC"
    ).fetchall()
    if not rows:
        print("No plans stored yet.")
        return
    print(f"\n── Plans ({len(rows)}) {'─'*38}")
    for r in rows:
        status = "✅" if r[1] else f"⏳{r[2]}/{N_CONFIRM}"
        last   = r[5][:10] if r[5] else "never"
        slots  = json.loads(r[6] or "[]")
        slot_s = f" [{','.join(slots)}]" if slots else ""
        print(f"  [{status}] {r[3]}✓ {r[4]}✗  {r[0][:44]:<44}{slot_s}  {last}")
    print()

def forget_plan_by_id(conn, pid):
    """Surgically evict a plan by exact ID, no fuzzy matching."""
    row = conn.execute("SELECT template FROM plans WHERE id=?", (pid,)).fetchone()
    if row:
        conn.execute("DELETE FROM plans WHERE id=?", (pid,))
        conn.commit()
        print(f"✅ Evicted plan: '{row[0]}'")
    else:
        print(f"No plan found with id: {pid}")

def list_plans_json(conn):
    """Return all plans as JSON for programmatic inspection (OpenClaw dispatcher)."""
    rows = conn.execute("""
        SELECT id, template, trusted, confirm_count, hits, failures,
               param_slots, last_used, created_at
        FROM plans ORDER BY hits DESC
    """).fetchall()
    cols = ["id","template","trusted","confirm_count","hits","failures",
            "param_slots","last_used","created_at"]
    return [dict(zip(cols, r)) for r in rows]

def forget_plan(conn, intent):
    template, _ = extract_params(intent)
    pid = plan_id(template)
    row = conn.execute("SELECT template FROM plans WHERE id=?", (pid,)).fetchone()
    if not row:
        best, _ = search_cache(conn, intent, threshold=0.5)
        if best:
            conn.execute("DELETE FROM plans WHERE id=?", (best["id"],))
            conn.commit()
            print(f"✅ Forgot: '{best['template']}'")
        else:
            print(f"No match for: '{intent}'")
    else:
        conn.execute("DELETE FROM plans WHERE id=?", (pid,))
        conn.commit()
        print(f"✅ Forgot: '{row[0]}'")

def run_check():
    print(f"── IMPRINT v{VERSION} check {'─'*30}")
    print(f"{'✅' if os.path.exists(WRANGLE) else '❌'} wrangle:    {WRANGLE}")
    # Check openclaw CLI is available and session exists
    try:
        r = subprocess.run([
            "openclaw", "agent", "--session-id", OPENCLAW_SESSION,
            "--message", "reply with the single word ok", "--json"
        ], capture_output=True, text=True, timeout=30)
        ok_resp = r.returncode == 0
        print(
            f"{'✅' if ok_resp else '❌'} OpenClaw:     session='{OPENCLAW_SESSION}' "
            f"{'reachable' if ok_resp else 'FAILED: ' + (r.stderr.strip()[:60] or 'unknown error')}"
        )
    except Exception as e:
        print(f"⚠️  OpenClaw:     session='{OPENCLAW_SESSION}' check failed ({e})")
    try:
        conn = init_db()
        p = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        a = conn.execute("SELECT COUNT(*) FROM app_cache").fetchone()[0]
        print(f"✅ SQLite:       {DB_PATH} ({p} plans, {a} apps)")
        conn.close()
    except Exception as e:
        print(f"❌ SQLite:       {e}")
    try:
        r = subprocess.run(["adb","devices"], capture_output=True, text=True, timeout=5)
        device_lines = [ln.strip() for ln in r.stdout.splitlines() if "\tdevice" in ln]
        target = f"localhost:{ADB_PORT}"
        connected = any(ln.startswith(target + "\t") for ln in device_lines) or bool(device_lines)
        detail = target if any(ln.startswith(target + "\t") for ln in device_lines) else (device_lines[0].split("\t")[0] if device_lines else "none")
        print(f"{'✅' if connected else '⚠️ '} ADB:          {'connected (' + detail + ')' if connected else 'no device'}")
    except Exception as e:
        print(f"⚠️  ADB:          {e}")
    print(f"\n  threshold={THRESHOLD}  confirm={N_CONFIRM}  max_steps={MAX_STEPS}  retries={MAX_RETRIES}  timeout={TASK_TIMEOUT}s  debug={'on' if DEBUG else 'off'}")
    print()

# ── CLI ───────────────────────────────────────────────────────────────────────

def cli():
    args = sys.argv[1:]
    if not args:
        print(f"IMPRINT v{VERSION}: Local Agentic Model")
        print("  ask '<task>' [--dry]  stats  list  forget '<task>'  apps  check")
        return 0

    cmd = args[0].lower()
    if cmd == "check":
        run_check(); return 0

    conn = init_db()

    if   cmd == "stats":      print_stats(conn)
    elif cmd == "queue":      show_queue(conn)
    elif cmd == "flush":      flush_queue(conn)
    elif cmd == "plans-json": print(json.dumps(list_plans_json(conn), indent=2))
    elif cmd == "forget-id":
        if len(args) < 2: print("Usage: imprint.py forget-id <plan_id>"); return 1
        forget_plan_by_id(conn, args[1])
    elif cmd == "list":   list_plans(conn)
    elif cmd == "apps":   refresh_apps(conn)
    elif cmd == "forget":
        if len(args) < 2: print("Usage: imprint.py forget '<task>'"); return 1
        forget_plan(conn, " ".join(args[1:]).strip("'\""))
    elif cmd == "ask":
        if len(args) < 2: print("Usage: imprint.py ask '<task>'"); return 1
        dry       = "--dry"       in args
        queued    = "--queue"     in args
        confirmed = "--confirmed" in args
        intent = " ".join(
            a for a in args[1:]
            if a not in ("--dry","--queue","--confirmed")
        ).strip("'\"")
        if queued:
            queue_task(conn, intent, dry_run=dry)
            result = {"queued": True, "intent": intent}
        else:
            result = route(intent, conn, dry_run=dry, confirmed=confirmed)
        print("\n" + json.dumps(result, indent=2))
    else:
        print(f"Unknown: {cmd}"); return 1

    conn.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(cli())
