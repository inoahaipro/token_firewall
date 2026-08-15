"""
wrangle.py: Android UI Controller v2.0.0 (IMPRINT execution adapter)
==========================================================

WHAT THIS DOES:
  Controls Chrome and Android apps using a text-first approach.

  TRACK 1 - BROWSER (Chrome via agent-browser + Cerebras):
    Uses agent-browser --cdp 9222 to get an accessibility tree snapshot
    of Chrome, feeds it as text to Cerebras GPT-OSS 120B, gets back an
    action (click ref / type / press key), executes it.

  TRACK 2 - NATIVE APPS (UIAutomator + Cerebras):
    Dumps the Android UI hierarchy as XML, ranks useful elements,
    feeds a compact structured summary to Cerebras, gets back an
    action (tap / type / swipe / keyevent / launch / done), executes it.

  TRACK 3 - NATIVE VISION (optional, currently disabled in auto-routing):
    Screenshot + vision LLM fallback for weird apps where UIAutomator is weak.

NOTES:
  - Text-first native control is the default because it is faster,
    cheaper, and less janky than screenshot vision for most apps.
  - Screen hashing and repeat detection help prevent dumb repeat loops.
  - Vision fallback uses Moondream via Ollama (no API key needed).
  - Run: python wrangle.py check  to validate setup before first use.

REQUIREMENTS:
  pip install requests
  ADBKeyboard APK installed and set as active IME:
    adb install ADBKeyboard.apk
    adb shell ime set com.android.adbkeyboard/.AdbIME
  Ollama + moondream for vision fallback (optional):
    ollama pull moondream

CONFIG (env vars):
  ADB_PORT                ADB port (default 5555; OpenClaw Termux uses 37383)
  CEREBRAS_KEY            Cerebras API key (required for text/browser tracks)
  WRANGLE_MAX_UI_ELEMENTS Max ranked UI elements sent to LLM (default 24)
  WRANGLE_ADB_RETRIES     Reconnect attempts on lost connection (default 3)
  OLLAMA_URL              Ollama endpoint (default http://localhost:11434)
  VISION_MODEL            Ollama vision model (default moondream)
  DEVICE_WIDTH / HEIGHT   Screen resolution (default 1080x2340 for S22+)

ADB CONNECTIVITY:
  Runs IN Termux on the device, connects via localhost ADB.
  When WiFi drops, adbd may kill the TCP socket even on loopback.
  ensure_connected() auto-reconnects before every operation.)
"""

import subprocess
import json
import requests
import time
import os
import base64
import hashlib
import re
import logging
import xml.etree.ElementTree as ET

# ── Config ────────────────────────────────────────────────────────────────────

# ADB_PORT: 5555 is the standard ADB-over-TCP default.
# OpenClaw Termux tunnel uses 45171: set ADB_PORT=45171 in that case.
ADB_PORT      = os.environ.get("ADB_PORT", "45171")
ADB           = f"adb -s localhost:{ADB_PORT}"
# CEREBRAS_KEY can be set via env; falls back to the key from openclaw.json
CEREBRAS_KEY  = os.environ.get("CEREBRAS_KEY")
CEREBRAS_URL  = "https://api.cerebras.ai/v1"
TEXT_MODEL    = "gpt-oss-120b"
SDCARD_SCREEN = "/sdcard/screen.png"
LOCAL_SCREEN  = os.path.expanduser("~/screen.png")
AB            = "npx agent-browser --cdp 9222"
MAX_UI_ELEMENTS = int(os.environ.get("WRANGLE_MAX_UI_ELEMENTS", "24"))
ADB_RETRIES     = int(os.environ.get("WRANGLE_ADB_RETRIES",     "3"))

log = logging.getLogger("wrangle")

# Screen resolution: change these if you are not on an S22+ (1080x2340).
DEVICE_WIDTH  = int(os.environ.get("DEVICE_WIDTH",  "1080"))
DEVICE_HEIGHT = int(os.environ.get("DEVICE_HEIGHT", "2340"))

# ── ADB ───────────────────────────────────────────────────────────────────────

def _adb_connect_once():
    """Single attempt to (re)connect ADB and forward CDP port."""
    subprocess.run(f"adb connect localhost:{ADB_PORT}",
                   shell=True, capture_output=True, timeout=8)
    subprocess.run(f"{ADB} forward tcp:9222 localabstract:chrome_devtools_remote",
                   shell=True, capture_output=True, timeout=8)

def check_connected():
    """Return True if ADB shows our localhost device online."""
    try:
        r = subprocess.run("adb devices", shell=True, capture_output=True,
                           text=True, timeout=5)
        return "device" in r.stdout and "localhost" in r.stdout
    except Exception:
        return False

def ensure_connected(retries=None, backoff=2.0):
    if retries is None:
        retries = ADB_RETRIES
    """
    Guarantee ADB is live before any command.

    Why this exists: adbd on Android often binds to the WiFi interface even
    for localhost connections. When the phone leaves WiFi (or WiFi drops
    briefly), the TCP socket dies, even though Termux and adbd are both
    on the same device. This function detects the dead socket and reconnects
    before every ADB operation so callers never see a stale connection.

    Returns True if connected, False if all retries exhausted.
    """
    if check_connected():
        return True
    for attempt in range(1, retries + 1):
        print(f"ADB: reconnecting (attempt {attempt}/{retries})...")
        try:
            _adb_connect_once()
        except Exception:
            pass
        if check_connected():
            print(f"ADB: reconnected to localhost:{ADB_PORT}")
            return True
        if attempt < retries:
            time.sleep(backoff * attempt)
    print(f"ADB: could not reconnect after {retries} attempts")
    return False

def reconnect():
    """Legacy alias, prefer ensure_connected() for new code."""
    ensure_connected()
    print(f"ADB: connected to localhost:{ADB_PORT}, CDP forwarded")

# ── Screenshot ────────────────────────────────────────────────────────────────

def screenshot():
    """Capture screen via ADB. Works on any visible app."""
    ensure_connected()
    subprocess.run(f"{ADB} shell screencap -p {SDCARD_SCREEN}",
                   shell=True, capture_output=True)
    subprocess.run(f"{ADB} pull {SDCARD_SCREEN} {LOCAL_SCREEN}",
                   shell=True, capture_output=True)
    with open(LOCAL_SCREEN, "rb") as f:
        return base64.b64encode(f.read()).decode()

def screenshot_and_save(path="/sdcard/DCIM/Screenshots/wrangle.png"):
    """Save screenshot to Gallery for human inspection."""
    subprocess.run(f"{ADB} shell screencap -p {path}", shell=True)
    subprocess.run(
        f"{ADB} shell am broadcast "
        f"-a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
        f"-d file:///{path}",
        shell=True, capture_output=True
    )
    print(f"Saved to Gallery: {path}")

# ── agent-browser ─────────────────────────────────────────────────────────────

def snapshot(interactive_only=True):
    flag = "-i" if interactive_only else ""
    result = subprocess.run(
        f"{AB} snapshot {flag}",
        shell=True, capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip()

def browser_action(action, **kwargs):
    if action == "open":
        cmd = f"{AB} open {kwargs['url']}"
    elif action == "click":
        cmd = f"{AB} click {kwargs['ref']}"
    elif action == "fill":
        cmd = f'{AB} fill {kwargs["ref"]} "{kwargs["text"]}"'
    elif action == "press":
        cmd = f"{AB} press {kwargs['key']}"
    elif action == "scroll":
        cmd = f"{AB} scroll {kwargs.get('direction','down')} {kwargs.get('px', 500)}"
    elif action == "back":
        cmd = f"{AB} back"
    elif action == "screenshot":
        cmd = f"{AB} screenshot {kwargs.get('path','~/browser.png')}"
    elif action == "get":
        cmd = f"{AB} get {kwargs['what']} {kwargs.get('ref','')}"
    else:
        cmd = f"{AB} {action}"

    result = subprocess.run(cmd, shell=True, capture_output=True,
                            text=True, timeout=20)
    return result.stdout.strip() or result.stderr.strip()

# ── App Control ───────────────────────────────────────────────────────────────

# Known app aliases. Samsung-specific entries (camera, gallery) fail on non-Samsung devices.
# Use list_apps()/find_package() to discover components on other hardware.
APPS = {
    "chrome":   "com.android.chrome/com.google.android.apps.chrome.Main",
    "whatsapp": "com.whatsapp/com.whatsapp.HomeActivity",
    "settings": "com.android.settings/.Settings",
    "spotify":  "com.spotify.music/com.spotify.music.MainActivity",
    # Use package-only here; launch_app will use monkey so we don't have to
    # deal with the $HomeActivity component escaping.
    "youtube":  "com.google.android.youtube",
    # Facebook main app on your phone
    "facebook": "com.facebook.katana/com.facebook.katana.LoginActivity",
    # ChatGPT app on your phone
    "chatgpt":  "com.openai.chatgpt",
    "telegram": "org.telegram.messenger/org.telegram.messenger.DefaultIcon",
    "camera":   "com.sec.android.app.camera/com.sec.android.app.camera.Camera",       # Samsung only
    "gallery":  "com.sec.android.gallery3d/com.sec.android.gallery3d.app.GalleryActivity",  # Samsung only
    "maps":     "com.google.android.apps.maps/com.google.android.maps.MapsActivity",
}

def _resolve_launch_target(app_name):
    """
    Resolve app_name to a launchable component string.
    Priority: 1) APPS alias dict  2) cached launcher components  3) monkey fallback
    Returns (package_or_component, method_used)
    """
    name = app_name.lower().strip()

    # 1. Hardcoded aliases (fast path for common apps)
    if name in APPS:
        return APPS[name], "alias"

    # 2. Launcher component discovery via package manager
    try:
        r = subprocess.run(
            f"{ADB} shell cmd package query-activities "
            f"-a android.intent.action.MAIN -c android.intent.category.LAUNCHER",
            shell=True, capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            if name in line.lower():
                m = re.search(r"([A-Za-z0-9_.]+/[A-Za-z0-9_.\$]+)", line)
                if m:
                    return m.group(1), "launcher_query"
    except Exception:
        pass

    # 3. Package name match (partial) → monkey launch
    try:
        r = subprocess.run(
            f"{ADB} shell pm list packages",
            shell=True, capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            pkg = line.replace("package:", "").strip()
            if name in pkg.lower():
                return pkg, "monkey"
    except Exception:
        pass

    return app_name, "unknown"

def launch_app(app_name, url=None, wait=2):
    """Launch app by name. Returns package string for verification."""
    if url:
        # URL launch: Chrome or intent
        chrome = APPS.get("chrome", "com.android.chrome/com.google.android.apps.chrome.Main")
        subprocess.run(
            f'{ADB} shell am start -n {chrome} -d "{url}"',
            shell=True, capture_output=True
        )
        time.sleep(wait)
        return chrome.split("/")[0]

    target, method = _resolve_launch_target(app_name)
    log.debug(f"launch_app: {app_name!r} → {target!r} via {method}")

    # If we only have a package name (no /component), or the resolver
    # explicitly said "monkey", use monkey to avoid component escaping
    # issues (like the $HomeActivity in YouTube's launcher activity).
    if method == "monkey" or "/" not in target:
        subprocess.run(
            f"{ADB} shell monkey -p {target} -c android.intent.category.LAUNCHER 1",
            shell=True, capture_output=True
        )
    else:
        subprocess.run(
            f"{ADB} shell am start -n {target}",
            shell=True, capture_output=True
        )

    time.sleep(wait)
    return target.split("/")[0]

def list_apps():
    """Return launchable apps as structured JSON-friendly records."""
    # Try launcher-resolvable packages first
    cmd = (
        f"{ADB} shell cmd package query-activities "
        f"-a android.intent.action.MAIN -c android.intent.category.LAUNCHER"
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)

    packages = set()
    if r.returncode == 0 and r.stdout.strip():
        for line in r.stdout.splitlines():
            line = line.strip()
            m = re.search(r'([A-Za-z0-9_$.]+/[A-Za-z0-9_$.\$]+)', line)
            if m:
                component = m.group(1)
                pkg = component.split('/')[0]
                packages.add(pkg)
            else:
                m2 = re.search(r'packageName=([A-Za-z0-9_$.]+)', line)
                if m2:
                    packages.add(m2.group(1))

    # Fallback to all packages if launcher query is unhelpful
    if not packages:
        r2 = subprocess.run(f"{ADB} shell pm list packages", shell=True, capture_output=True, text=True)
        for line in r2.stdout.splitlines():
            line = line.strip()
            if line.startswith('package:'):
                packages.add(line.split(':', 1)[1])

    app_records = []
    for pkg in sorted(packages):
        simple_label = pkg.split('.')[-1].replace('_', ' ')
        app_records.append({
            'label': simple_label,
            'package': pkg,
            'launchable': True,
            'known_aliases': [alias for alias, target in APPS.items() if target.startswith(pkg + '/') or target == pkg],
        })

    return {'count': len(app_records), 'apps': app_records}


def find_package(name):
    """Return likely package matches for a search term as JSON-friendly data."""
    search = (name or '').strip().lower()
    apps = list_apps().get('apps', [])
    matches = []
    for app in apps:
        hay = ' '.join([app.get('label', ''), app.get('package', ''), ' '.join(app.get('known_aliases', []))]).lower()
        if search in hay:
            matches.append(app)

    # Fallback: pull raw package list in Python, no shell grep (safe on all envs)
    if not matches and search:
        try:
            r = subprocess.run(f"{ADB} shell pm list packages",
                               shell=True, capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                pkg = line.replace('package:', '').strip()
                if pkg and search in pkg.lower():
                    matches.append({'label': pkg.split('.')[-1], 'package': pkg,
                                    'launchable': True, 'known_aliases': []})
        except Exception:
            pass

    return {'query': name, 'count': len(matches), 'matches': matches[:30]}

# ── ADB Input ────────────────────────────────────────────────────────────────

def tap(x, y, delay=0.8):
    subprocess.run(f"{ADB} shell input tap {x} {y}", shell=True)
    time.sleep(delay)

def swipe(x1, y1, x2, y2, ms=300, delay=0.5):
    subprocess.run(f"{ADB} shell input swipe {x1} {y1} {x2} {y2} {ms}", shell=True)
    time.sleep(delay)

def scroll_down(amount=800):
    cx = DEVICE_WIDTH // 2
    swipe(cx, int(DEVICE_HEIGHT * 0.60), cx, int(DEVICE_HEIGHT * 0.60) - amount)

def scroll_up(amount=800):
    cx = DEVICE_WIDTH // 2
    swipe(cx, int(DEVICE_HEIGHT * 0.26), cx, int(DEVICE_HEIGHT * 0.26) + amount)

def check_adbkeyboard():
    """Warn if ADBKeyboard is not the active IME."""
    r = subprocess.run(f"{ADB} shell settings get secure default_input_method",
                       shell=True, capture_output=True, text=True)
    ime = r.stdout.strip()
    if "adbkeyboard" not in ime.lower():
        print(f"⚠️  ADBKeyboard not active (current IME: {ime}). "
              "Run: adb shell ime set com.android.adbkeyboard/.AdbIME")
        return False
    return True


def clear_focused_input_if_any(max_deletes: int = 80) -> bool:
    """If a focused editable field currently has text, clear it before typing.

    Uses wrangle's own get_phone_state so we don't depend on IMPRINT here.
    Returns True if we detected text and sent deletes, False otherwise.
    """
    try:
        state = get_phone_state()
    except Exception:
        return False

    elements = state.get("elements", []) or []
    has_text = False
    for el in elements:
        cls = (el.get("class") or "").lower()
        rid = (el.get("resource-id") or el.get("resource_id") or "").lower()
        label = (el.get("label") or el.get("text") or "").strip()

        # Treat typical text inputs as editable even if "editable" flag is false
        is_text_input = (
            el.get("editable")
            or "edittext" in cls
            or "autocompletetextview" in cls
            or "textarea" in cls
            or "prompt-textarea" in rid
        )
        if not is_text_input:
            continue

        if not el.get("focused") and not el.get("focusable"):
            continue
        if label:
            has_text = True
            break

    if not has_text:
        return False

    try:
        keyevent("KEYCODE_MOVE_END", delay=0.05)
    except Exception:
        pass
    for _ in range(max_deletes):
        try:
            keyevent("KEYCODE_DEL", delay=0.02)
        except Exception:
            break
    return True


def type_text(text, delay=0.3):
    """Type text via adb input text (no IME dependency).

    This avoids ADBKeyboard flakiness in some apps (e.g. Facebook composer)
    by sending input directly, regardless of the current soft keyboard.
    Note: adb input text has limited escaping; this is tuned for short
    alphanumeric/space strings like queries and chat messages.
    """
    # Basic escaping: spaces -> %s, percent -> %%
    safe = text.replace('%', '%%').replace(' ', '%s')
    subprocess.run(f"{ADB} shell input text {safe}", shell=True)
    time.sleep(delay)

def keyevent(key, delay=0.3):
    subprocess.run(f"{ADB} shell input keyevent {key}", shell=True)
    time.sleep(delay)

def press_back():  keyevent("KEYCODE_BACK")
def press_home():  keyevent("KEYCODE_HOME")
def press_enter(): keyevent("KEYCODE_ENTER")

# ── UI Parsing / Ranking ──────────────────────────────────────────────────────

def parse_bounds(bounds):
    if not bounds:
        return None
    try:
        coords = bounds.replace("][", ",").replace("[", "").replace("]", "")
        x1, y1, x2, y2 = map(int, coords.split(","))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        return {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "cx": cx, "cy": cy,
            "w": max(0, x2 - x1),
            "h": max(0, y2 - y1),
        }
    except Exception:
        return None

def clean_label(*parts):
    for part in parts:
        if part:
            value = re.sub(r"\s+", " ", part).strip()
            if value:
                return value
    return ""

def element_score(el, task=""):
    score = 0.0
    label = el.get("label", "").lower()
    resource = el.get("resource_id", "").lower()
    cls = el.get("class", "")
    clickable = el.get("clickable", False)
    checkable = el.get("checkable", False)
    editable = el.get("editable", False)
    selected = el.get("selected", False)

    if clickable:
        score += 5
    if editable:
        score += 5
    if checkable:
        score += 3
    if selected:
        score += 1
    if label:
        score += min(4, max(1, len(label) / 12))
    if resource:
        score += 1

    if "button" in cls or "imagebutton" in cls:
        score += 2
    if "edittext" in cls:
        score += 3
    if "switch" in cls or "checkbox" in cls:
        score += 2

    # De-prioritize giant layout containers
    area = el.get("area", 0)
    if area > 700_000:
        score -= 4
    elif area > 300_000:
        score -= 2

    # Favor lower half slightly for common actions, but not too much
    cy = el.get("center", [DEVICE_WIDTH // 2, DEVICE_HEIGHT // 2])[1]
    if 700 <= cy <= 2200:
        score += 0.5

    if task:
        task_words = {w for w in re.findall(r"[a-zA-Z0-9]+", task.lower()) if len(w) > 2}
        if task_words:
            hay = f"{label} {resource}".lower()
            overlaps = sum(1 for w in task_words if w in hay)
            score += overlaps * 2.5

    return round(score, 2)

def collect_ui_elements(task=""):
    """Return ranked visible UI elements plus a stable screen hash.

    Retries up to 3 times if UIAutomator returns an empty dump. This happens
    when the screen is mid-transition, locked, or the system is briefly busy.
    """
    for attempt in range(1, 4):
        # Wake screen first: UIAutomator returns empty on a locked/sleeping display
        subprocess.run(f"{ADB} shell input keyevent KEYCODE_WAKEUP",
                       shell=True, capture_output=True)
        subprocess.run(
            f"{ADB} shell uiautomator dump /sdcard/ui.xml",
            shell=True, capture_output=True, timeout=10
        )
        local_xml = os.path.expanduser("~/ui.xml")
        subprocess.run(
            f"{ADB} pull /sdcard/ui.xml {local_xml}",
            shell=True, capture_output=True, timeout=10
        )

        try:
            tree = ET.parse(local_xml)
            root = tree.getroot()
        except Exception as e:
            if attempt < 3:
                time.sleep(0.8)
                continue
            return {
                "screen_hash": "parse_error",
                "elements": [],
                "raw_count": 0,
                "error": f"XML parse error: {e}",
            }

        elements = []
        raw_count = 0
        for idx, node in enumerate(root.iter("node"), start=1):
            raw_count += 1
            cls = node.get("class", "").split(".")[-1]
            text = node.get("text", "").strip()
            desc = node.get("content-desc", "").strip()
            resource = node.get("resource-id", "").split("/")[-1]
            package = node.get("package", "")
            bounds = node.get("bounds", "")
            clickable = node.get("clickable", "false") == "true"
            enabled = node.get("enabled", "false") == "true"
            focusable = node.get("focusable", "false") == "true"
            focused = node.get("focused", "false") == "true"
            editable = node.get("editable", "false") == "true"
            checkable = node.get("checkable", "false") == "true"
            checked = node.get("checked", "false") == "true"
            selected = node.get("selected", "false") == "true"
            scrollable = node.get("scrollable", "false") == "true"

            if not enabled:
                continue

            label = clean_label(text, desc, resource)
            if not label and not clickable and not editable and not checkable:
                continue

            parsed = parse_bounds(bounds)
            if not parsed:
                continue

            element = {
                "id": idx,
                "label": label,
                "class": cls,
                "resource_id": resource,
                "content_desc": desc,
                "package": package,
                "clickable": clickable,
                "focusable": focusable,
                "focused": focused,
                "enabled": enabled,
                "editable": editable,
                "checkable": checkable,
                "checked": checked,
                "selected": selected,
                "scrollable": scrollable,
                "bounds": bounds,
                "center": [parsed["cx"], parsed["cy"]],
                "area": parsed["w"] * parsed["h"],
            }
            element["score"] = element_score(element, task)
            elements.append(element)

        # If UIAutomator returned zero useful nodes, the screen is likely
        # mid-transition. Retry after a short wait.
        if not elements and attempt < 3:
            log.debug(f"collect_ui_elements: empty dump (attempt {attempt}/3), retrying...")
            time.sleep(0.8)
            continue

        # Got elements, no need to retry
        break

    elements.sort(key=lambda e: e["score"], reverse=True)
    ranked = elements[:MAX_UI_ELEMENTS]

    # Normalized for stable screen hash: uses only content fields, not coords
    normalized = [
        {"label": e["label"], "class": e["class"],
         "resource_id": e.get("resource_id",""), "clickable": e["clickable"]}
        for e in ranked
    ]
    screen_hash = hashlib.sha1(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:12]

    return {
        "screen_hash": screen_hash,
        "elements": ranked,
        "raw_count": raw_count,
        "error": None,
    }

def render_ui_text(state):
    elements = state.get("elements", [])
    if not elements:
        return "No elements found"

    lines = []
    for el in elements:
        flags = []
        if el["clickable"]:
            flags.append("click")
        if el["editable"]:
            flags.append("edit")
        if el["checkable"]:
            flags.append("check")
        if el["checked"]:
            flags.append("checked")
        if el["selected"]:
            flags.append("selected")
        if el["scrollable"]:
            flags.append("scroll")
        flag_str = ",".join(flags) if flags else "plain"
        lines.append(
            f'#{el["id"]} [{el["class"].upper()}|{flag_str}] '
            f'"{el["label"]}" center=({el["center"][0]},{el["center"][1]}) bounds={el["bounds"]}'
        )
    return "\n".join(lines)

def ui_dump(task=""):
    """Backward-compatible text dump, now ranked and compact."""
    state = collect_ui_elements(task=task)
    if state.get("error"):
        return state["error"]
    return render_ui_text(state)

def _get_foreground_app():
    """
    Detect foreground app across Android versions.

    Android 10 and earlier:  dumpsys activity activities → mResumedActivity
    Android 11+:              mResumedActivity may be gone; fall back to
                              dumpsys window windows → mCurrentFocus, then
                              dumpsys activity top → ACTIVITY line.
    Returns package string or empty string on failure.
    """
    # Method 1: activity stack (reliable on most versions)
    try:
        r = subprocess.run(
            f"{ADB} shell dumpsys activity activities",
            shell=True, capture_output=True, text=True, timeout=6
        )
        for line in r.stdout.splitlines():
            if "ResumedActivity" in line:
                m = re.search(r"([A-Za-z0-9_.]+)/[A-Za-z0-9_.]+", line)
                if m:
                    return m.group(1)
    except Exception:
        pass

    # Method 2: window focus (works on Android 12/13/14)
    try:
        r = subprocess.run(
            f"{ADB} shell dumpsys window windows",
            shell=True, capture_output=True, text=True, timeout=6
        )
        for line in r.stdout.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                m = re.search(r"([A-Za-z0-9_.]+)/[A-Za-z0-9_.]+", line)
                if m:
                    return m.group(1)
    except Exception:
        pass

    # Method 3: activity top (last resort, slower but universal)
    try:
        r = subprocess.run(
            f"{ADB} shell dumpsys activity top",
            shell=True, capture_output=True, text=True, timeout=6
        )
        for line in r.stdout.splitlines():
            if "ACTIVITY" in line:
                m = re.search(r"([A-Za-z0-9_.]+)/[A-Za-z0-9_.]+", line)
                if m:
                    return m.group(1)
    except Exception:
        pass

    return ""

def get_phone_state(task=""):
    state = collect_ui_elements(task=task)
    if state.get("error"):
        return {
            "screen_hash": state.get("screen_hash", "error"),
            "screen_summary": state["error"],
            "elements": [],
            "raw_count": state.get("raw_count", 0),
        }

    elements = []
    for el in state["elements"]:
        role = "input" if el["editable"] else "button" if el["clickable"] else el["class"].lower()
        cx, cy = el["center"]
        elements.append({
            # Identity: IMPRINT resolver targets by these fields
            "id":           el["id"],
            "text":         el.get("resource_id","") and el["label"] or el["label"],
            "label":        el["label"],
            "content-desc": el.get("content_desc",""),
            "resource-id":  el.get("resource_id",""),
            "class":        el["class"],
            # State flags
            "role":         role,
            "clickable":    el["clickable"],
            "editable":     el["editable"],
            "focusable":    el.get("focusable", False),
            "focused":      el.get("focused", False),
            "enabled":      el.get("enabled", True),
            "scrollable":   el.get("scrollable", False),
            "checkable":    el["checkable"],
            "checked":      el["checked"],
            "selected":     el["selected"],
            # Position: absolute + normalized
            "x":            cx,
            "y":            cy,
            "x_norm":       round(cx / DEVICE_WIDTH,  4),
            "y_norm":       round(cy / DEVICE_HEIGHT, 4),
            "center":       el["center"],
            "bounds":       el["bounds"],
            "score":        el["score"],
        })

    fg_app = _get_foreground_app()
    summary = f"{len(elements)} ranked elements from {state['raw_count']} raw UI nodes"
    return {
        "screen_hash": state["screen_hash"],
        "foreground_app": fg_app,
        "screen_summary": summary,
        "elements": elements,
        "raw_count": state["raw_count"],
    }

# ── Native Cerebras Reasoning ─────────────────────────────────────────────────

def ask_cerebras_native(task, phone_state, history, loop_state=None):
    loop_state = loop_state or {}
    ui_text = render_ui_text(phone_state)
    stuck_hint = ""
    if loop_state.get("repeat_count", 0) >= 2:
        stuck_hint = (
            f"\nWarning: the screen has not changed for {loop_state['repeat_count']} consecutive checks. "
            f"The last action was {json.dumps(loop_state.get('last_action', {}))}. "
            "Choose a DIFFERENT action unless repeating is clearly necessary."
        )

    fg = phone_state.get("foreground_app", "")
    fg_line = f"Foreground app: {fg}\n" if fg else ""
    prompt = (
        "You are controlling an Android app via structured UI element coordinates.\n"
        f"Task: {task}\n"
        f"{fg_line}"
        f"Screen hash: {phone_state.get('screen_hash','unknown')}\n"
        f"Raw node count: {phone_state.get('raw_count', 0)}\n"
        f"Ranked element count: {len(phone_state.get('elements', []))}\n"
        f"{stuck_hint}\n\n"
        "Current ranked screen elements:\n"
        f"{ui_text}\n\n"
        "Rules:\n"
        "- Prefer tapping a clearly relevant clickable element by center coordinates.\n"
        "- If the screen seems stuck, try a different action like back or scroll.\n"
        "- Only type when an input is likely focused or editable.\n"
        "- Return ONLY valid JSON, no markdown.\n\n"
        "Allowed formats:\n"
        '{"action":"tap","x":540,"y":1200,"reason":"tapping Wi-Fi toggle","target_id":17}\n'
        '{"action":"type","text":"hello","reason":"typing in search box"}\n'
        '{"action":"swipe","x1":540,"y1":1400,"x2":540,"y2":600,"ms":300,"reason":"scrolling"}\n'
        '{"action":"keyevent","key":"KEYCODE_BACK","reason":"going back"}\n'
        '{"action":"back","reason":"going back"}\n'
        '{"action":"scroll","direction":"down","amount":800,"reason":"scrolling"}\n'
        '{"action":"launch","app":"settings","reason":"need settings app"}\n'
        '{"action":"done","reason":"task complete"}'
    )

    messages = history + [{"role": "user", "content": prompt}]

    resp = requests.post(
        f"{CEREBRAS_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {CEREBRAS_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": TEXT_MODEL,
            "messages": messages,
            "max_tokens": 256,
            "temperature": 0.1
        },
        timeout=30
    )
    resp.raise_for_status()

    msg = resp.json()["choices"][0]["message"]
    raw = msg.get("content") or msg.get("reasoning") or ""
    raw = raw.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise ValueError(f"No JSON in native LLM response: {raw[:200]}")
    raw = m.group(0)
    action = json.loads(raw)
    history_updated = messages + [{"role": "assistant", "content": raw}]
    return action, history_updated

def read_screen():
    print("\n── Reading screen ──")
    phone_state = get_phone_state()
    if not phone_state["elements"]:
        print("(empty screen)")
        return

    resp = requests.post(
        f"{CEREBRAS_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {CEREBRAS_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": TEXT_MODEL,
            "messages": [{
                "role": "user",
                "content": (
                    "Summarize the key information visible on this Android screen "
                    "in plain English. Be concise.\n\n" + render_ui_text(phone_state)
                )
            }],
            "max_tokens": 512,
            "temperature": 0.1
        },
        timeout=30
    )
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    result = msg.get("content") or msg.get("reasoning") or "(no response)"
    print(f"\n{result}\n")

def run_native_text(task, app=None, max_steps=20):
    ensure_connected()
    if app:
        launch_app(app)
        time.sleep(1)

    print(f"\n{'='*50}")
    print(f"NATIVE TASK: {task}")
    print(f"{'='*50}")

    history = []
    previous_hash = None
    repeat_count = 0
    last_action = None

    for step in range(max_steps):
        print(f"\n[Step {step + 1}/{max_steps}]")

        phone_state = get_phone_state(task=task)
        if not phone_state["elements"]:
            print("Empty UI dump, retrying...")
            time.sleep(1)
            phone_state = get_phone_state(task=task)

        current_hash = phone_state.get("screen_hash")
        if current_hash == previous_hash:
            repeat_count += 1
        else:
            repeat_count = 0

        print(
            f"UI: {len(phone_state['elements'])} ranked / {phone_state.get('raw_count', 0)} raw "
            f"| hash={current_hash} | repeats={repeat_count}"
        )

        if repeat_count >= 4:
            print(f"\n⚠️  Screen stuck for {repeat_count} steps, aborting to prevent runaway loop")
            break

        loop_state = {
            "repeat_count": repeat_count,
            "last_action": last_action,
        }

        if len(history) > 20:
            history = history[-20:]

        try:
            action, history = ask_cerebras_native(task, phone_state, history, loop_state)
        except Exception as e:
            print(f"LLM error: {e}")
            break

        print(f"→ {action.get('action')}, {action.get('reason','')}")

        a = action["action"]
        if a == "tap":
            tap(action["x"], action["y"])
        elif a == "type":
            type_text(action["text"])
        elif a == "swipe":
            swipe(action["x1"], action["y1"],
                  action["x2"], action["y2"],
                  action.get("ms", 300))
        elif a == "keyevent":
            keyevent(action["key"])
        elif a == "back":
            press_back()
        elif a == "scroll":
            direction = action.get("direction", "down")
            amount = int(action.get("amount", 800))
            scroll_up(amount) if direction == "up" else scroll_down(amount)
        elif a == "launch":
            launch_app(action["app"])
            time.sleep(1)
        elif a == "done":
            print(f"\n✅ DONE: {action.get('reason','')}")
            read_screen()
            break

        last_action = action
        previous_hash = current_hash
        time.sleep(1.2)

    else:
        print(f"\n⚠️  Max steps reached ({max_steps})")
        read_screen()

# ── Browser Reasoning ─────────────────────────────────────────────────────────

def ask_cerebras(task, ui_tree, history):
    prompt = (
        f"You are controlling Chrome on Android via accessibility refs.\n"
        f"Task: {task}\n\n"
        f"Current page interactive elements:\n{ui_tree}\n\n"
        "Pick the single best next action. Reply ONLY in JSON, no markdown:\n"
        '{"action":"click","ref":"@e12","reason":"clicking search box"}\n'
        '{"action":"fill","ref":"@e13","text":"weather New York","reason":"typing"}\n'
        '{"action":"press","key":"Enter","reason":"submitting"}\n'
        '{"action":"open","url":"https://google.com","reason":"navigating"}\n'
        '{"action":"scroll","direction":"down","reason":"more content below"}\n'
        '{"action":"back","reason":"going back"}\n'
        '{"action":"done","reason":"task complete"}'
    )

    messages = history + [{"role": "user", "content": prompt}]

    resp = requests.post(
        f"{CEREBRAS_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {CEREBRAS_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": TEXT_MODEL,
            "messages": messages,
            "max_tokens": 256,
            "temperature": 0.1
        },
        timeout=30
    )
    resp.raise_for_status()

    raw = resp.json()["choices"][0]["message"]["content"]
    raw = raw.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise ValueError(f"No JSON in browser LLM response: {raw[:200]}")
    raw = m.group(0)
    action = json.loads(raw)
    history_updated = messages + [{"role": "assistant", "content": raw}]
    return action, history_updated

# ── Vision LLM (optional fallback) ────────────────────────────────────────────

OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VISION_MODEL = os.environ.get("VISION_MODEL", "moondream")

def ask_vision(task, img_b64, history):
    """
    Vision fallback via Moondream on Ollama (local, no API key needed).

    Architecture note: use this as an interrogator, not a planner.
    Ask it "what is visible?" or "is X present?" and feed the answer
    back to the cloud LLM (Cerebras) to decide the action.

    Switch models without code changes: export VISION_MODEL=llava
    Switch endpoint: export OLLAMA_URL=http://192.168.x.x:11434
    """
    prompt = (
        f"Task: {task}\n\n"
        f"Screen resolution {DEVICE_WIDTH}x{DEVICE_HEIGHT}. "
        "Reply ONLY with a single JSON action, no markdown:\n"
        '{"action":"tap","x":540,"y":1200,"reason":"..."}\n'
        '{"action":"type","text":"...","reason":"..."}\n'
        '{"action":"swipe","x1":540,"y1":1400,"x2":540,"y2":600,"ms":300,"reason":"..."}\n'
        '{"action":"keyevent","key":"KEYCODE_BACK","reason":"..."}\n'
        '{"action":"back","reason":"..."}\n'
        '{"action":"scroll","direction":"down","amount":800,"reason":"..."}\n'
        '{"action":"launch","app":"chrome","reason":"..."}\n'
        '{"action":"done","reason":"..."}'
    )
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": VISION_MODEL,
            "prompt": prompt,
            "images": [img_b64],
            "stream": False,
        },
        timeout=60
    )
    resp.raise_for_status()
    raw = resp.json().get("response", "").strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise ValueError(f"No JSON in vision response: {raw[:200]}")
    action = json.loads(m.group(0))
    updated_history = history + [{"role": "assistant", "content": raw}]
    return action, updated_history

# ── Browser Agent Loop ────────────────────────────────────────────────────────

def run_browser(task, start_url=None, max_steps=20):
    ensure_connected()

    if start_url:
        launch_app("chrome", url=start_url)
        time.sleep(2)

    print(f"\n{'='*50}")
    print(f"BROWSER TASK: {task}")
    print(f"{'='*50}")

    history = []

    for step in range(max_steps):
        print(f"\n[Step {step + 1}/{max_steps}]")

        ui_tree = snapshot(interactive_only=True)
        if not ui_tree:
            print("Empty snapshot, re-forwarding CDP...")
            subprocess.run(
                f"{ADB} forward tcp:9222 localabstract:chrome_devtools_remote",
                shell=True, capture_output=True
            )
            time.sleep(1)
            ui_tree = snapshot()

        print(f"UI: {len(ui_tree.splitlines())} elements")

        if len(history) > 20:
            history = history[-20:]

        try:
            action, history = ask_cerebras(task, ui_tree, history)
        except Exception as e:
            print(f"LLM error: {e}")
            break

        print(f"→ {action.get('action')} {action.get('ref','')}{action.get('url','')}{action.get('text','')}, {action.get('reason','')}")

        a = action["action"]
        if a == "click":
            browser_action("click", ref=action["ref"])
        elif a == "fill":
            browser_action("fill", ref=action["ref"], text=action["text"])
        elif a == "press":
            browser_action("press", key=action["key"])
        elif a == "open":
            launch_app("chrome", url=action["url"])
            time.sleep(2)
        elif a == "scroll":
            browser_action("scroll", direction=action.get("direction", "down"))
        elif a == "back":
            browser_action("back")
        elif a == "done":
            print(f"\n✅ DONE: {action.get('reason','')}")
            break

        time.sleep(1.5)

    else:
        print(f"\n⚠️  Max steps reached ({max_steps})")

# ── Native Vision Agent Loop (optional) ──────────────────────────────────────

def run_native(task, app=None, max_steps=20):
    """Vision fallback loop via Moondream on Ollama. No API key required."""
    ensure_connected()
    if app:
        launch_app(app)

    print(f"\n{'='*50}")
    print(f"NATIVE VISION TASK: {task}")
    print(f"{'='*50}")

    history = []
    previous_hash = None
    repeat_count = 0

    for step in range(max_steps):
        print(f"\n[Step {step + 1}/{max_steps}]")
        img = screenshot()
        current_hash = hashlib.sha1(img[:512].encode()).hexdigest()[:12]
        repeat_count = repeat_count + 1 if current_hash == previous_hash else 0
        if repeat_count >= 4:
            print(f"\n⚠️  Vision loop stuck {repeat_count} steps, aborting")
            break
        previous_hash = current_hash

        if len(history) > 20:
            history = history[-20:]

        try:
            action, history = ask_vision(task, img, history)
        except Exception as e:
            print(f"LLM error: {e}")
            break

        print(f"→ {action.get('action')}, {action.get('reason','')}")
        a = action["action"]
        if a == "tap":
            tap(action["x"], action["y"])
        elif a == "type":
            type_text(action["text"])
        elif a == "swipe":
            swipe(action["x1"], action["y1"], action["x2"], action["y2"], action.get("ms", 300))
        elif a == "keyevent":
            keyevent(action["key"])
        elif a == "back":
            press_back()
        elif a == "scroll":
            direction = action.get("direction", "down")
            amount = int(action.get("amount", 800))
            scroll_up(amount) if direction == "up" else scroll_down(amount)
        elif a == "launch":
            launch_app(action["app"])
            time.sleep(1)
        elif a == "done":
            print(f"\n✅ DONE: {action.get('reason','')}")
            break
        time.sleep(1)

# ── Auto-detect ───────────────────────────────────────────────────────────────

def run(task, app=None, url=None, max_steps=20):
    """
    Auto-detect which track to use.

    - app='chrome' or url set → browser track
    - anything else           → native text track (default)

    Vision is intentionally NOT auto-selected right now.
    Keep the screenshot path around as an explicit fallback only.
    """
    if app == "chrome" or url:
        run_browser(task, start_url=url, max_steps=max_steps)
    else:
        run_native_text(task, app=app, max_steps=max_steps)

# ── CLI Tool Wrapper ─────────────────────────────────────────────────────────

def perform_action(action):
    """Execute a structured phone action and return a JSON-safe result.

    Supported actions:
      tap        {"action":"tap","x":540,"y":1200}
      type       {"action":"type","text":"hello"}
      swipe      {"action":"swipe","x1":100,"y1":800,"x2":100,"y2":200,"ms":300}
      scroll     {"action":"scroll","direction":"down","amount":800}
      keyevent   {"action":"keyevent","key":"KEYCODE_ENTER"}
      back       {"action":"back"}
      launch     {"action":"launch","app":"youtube"}
      open_url   {"action":"open_url","url":"https://m.youtube.com/..."}
      done       {"action":"done","reason":"task complete"}

    For `type`, temporarily switches to ADBKeyboard (base64 broadcast) then
    restores the original IME so your normal keyboard comes back.
    """
    a = (action or {}).get("action")
    if not a:
        return {"ok": False, "error": "missing action"}

    if a == "tap":
        try:
            x = int(action["x"])
            y = int(action["y"])
        except Exception:
            return {"ok": False, "error": "tap requires integer x and y"}
        tap(x, y)
        return {"ok": True, "executed": "tap", "x": x, "y": y}

    if a == "type":
        text_val = str(action.get("text", ""))
        if not text_val:
            return {"ok": False, "error": "type requires non-empty text"}

        # Capture current IME, switch to ADBKeyboard, then re-focus an
        # input-like field before typing so we don't lose focus to "Done".
        try:
            ime_res = subprocess.run(
                f"{ADB} shell settings get secure default_input_method",
                shell=True, capture_output=True, text=True, timeout=4,
            )
            original_ime = ime_res.stdout.strip() or None
        except Exception:
            original_ime = None

        subprocess.run(
            f"{ADB} shell ime set com.android.adbkeyboard/.AdbIME",
            shell=True, capture_output=True, text=True,
        )
        time.sleep(0.3)

        # After IME switch, re-focus a likely input field.
        try:
            state = get_phone_state()
            els = state.get("elements", []) or []
            target_el = None
            for el in els:
                cls = (el.get("class") or "").lower()
                rid = (el.get("resource-id") or el.get("resource_id") or "").lower()
                if "edittext" in cls or "autocompletetextview" in cls or "textarea" in cls:
                    target_el = el
                    break
                if "prompt-textarea" in rid:
                    target_el = el
                    break
            if target_el:
                tap(int(target_el["x"]), int(target_el["y"]), delay=0.2)
        except Exception:
            pass

        clear_focused_input_if_any()
        type_text(text_val)

        if original_ime:
            subprocess.run(
                f"{ADB} shell ime set {original_ime}",
                shell=True, capture_output=True, text=True,
            )

        return {"ok": True, "executed": "type", "text": text_val}

    if a == "swipe":
        try:
            x1 = int(action["x1"]); y1 = int(action["y1"])
            x2 = int(action["x2"]); y2 = int(action["y2"])
        except Exception:
            return {"ok": False, "error": "swipe requires x1,y1,x2,y2"}
        ms = int(action.get("ms", 300))
        swipe(x1, y1, x2, y2, ms=ms, delay=0)
        return {"ok": True, "executed": "swipe"}

    if a == "scroll":
        direction = action.get("direction", "down")
        amount = int(action.get("amount", 800))
        scroll_up(amount) if direction == "up" else scroll_down(amount)
        return {"ok": True, "executed": "scroll", "direction": direction}

    if a == "keyevent":
        key = action.get("key", "")
        if not key:
            return {"ok": False, "error": "keyevent requires key"}
        keyevent(key)
        return {"ok": True, "executed": "keyevent", "key": key}

    if a == "back":
        press_back()
        return {"ok": True, "executed": "back"}

    if a == "launch":
        app_name = action.get("app", "")
        if not app_name:
            return {"ok": False, "error": "launch requires app name"}
        url = action.get("url")
        pkg = launch_app(app_name, url=url)
        return {"ok": True, "executed": "launch", "app": app_name, "package": pkg}

    if a == "open_url":
        url = action.get("url", "")
        if not url:
            return {"ok": False, "error": "open_url requires url"}
        launch_app("chrome", url=url)
        return {"ok": True, "executed": "open_url", "url": url}

    if a == "done":
        return {"ok": True, "executed": "done", "reason": action.get("reason", "")}

    return {"ok": False, "error": f"unsupported action: {a}"}


def run_check():
    """Validate setup: ADB connection, ADBKeyboard IME, optional Cerebras key, Ollama."""
    import urllib.request as _req
    import json as _j
    ok = True
    print("── wrangle setup check ──")

    connected = check_connected()
    print(f"{'✅' if connected else '❌'} ADB: localhost:{ADB_PORT} "
          f"{'connected' if connected else 'NOT connected'}")
    if not connected:
        print(f"   → adb connect localhost:{ADB_PORT}")
        ok = False

    if connected:
        r = subprocess.run(f"{ADB} shell settings get secure default_input_method",
                           shell=True, capture_output=True, text=True)
        ime = r.stdout.strip()
        ime_ok = "adbkeyboard" in ime.lower()
        print(f"{'✅' if ime_ok else '⚠️ '} ADBKeyboard: "
              f"{'active' if ime_ok else f'NOT active (current: {ime})'}")
        if not ime_ok:
            print("   → adb shell ime set com.android.adbkeyboard/.AdbIME")

    key_ok = bool(CEREBRAS_KEY)
    print(f"{'✅' if key_ok else '⚠️ '} CEREBRAS_KEY: {'set' if key_ok else 'NOT set (optional in v2)'}")
    if not key_ok:
        print("   → only needed for direct wrangle text/browser tracks")

    try:
        with _req.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            tags = _j.loads(resp.read())
        models = [m["name"].split(":")[0] for m in tags.get("models", [])]
        model_ok = VISION_MODEL in models
        print(f"{'✅' if model_ok else '⚠️ '} Ollama: reachable | "
              f"{VISION_MODEL}: {'available' if model_ok else 'NOT pulled'}")
        if not model_ok:
            print(f"   → ollama pull {VISION_MODEL}")
    except Exception as e:
        print(f"⚠️  Ollama: unreachable at {OLLAMA_URL} ({e})")
        print("   → Vision fallback unavailable (text track still works fine)")

    print()
    print("✅ Core setup looks good." if ok else "❌ Fix the above before running.")
    return 0 if ok else 1

def print_json(data):
    print(json.dumps(data, ensure_ascii=False))

def cli():
    import argparse

    parser = argparse.ArgumentParser(
        description="OpenClaw Android phone controller tool (text-first)."
    )
    sub = parser.add_subparsers(dest="command")

    p_state = sub.add_parser("get_state", help="Return ranked current phone UI state as JSON")
    p_state.add_argument("--task", default="", help="Task hint for element ranking")

    p_action = sub.add_parser("do_action", help="Execute a JSON action on the phone")
    p_action.add_argument("--json", required=True, dest="action_json", help="Action JSON payload")

    p_run = sub.add_parser("run", help="Run the built-in loop")
    p_run.add_argument("task", help="Task to complete")
    p_run.add_argument("--app", default=None, help="App name or component")
    p_run.add_argument("--url", default=None, help="URL for Chrome/browser track")
    p_run.add_argument("--max-steps", type=int, default=20, help="Maximum loop steps")

    p_read = sub.add_parser("read_screen", help="Summarize current screen using text model")

    p_pkg = sub.add_parser("find_package", help="Search installed packages by name")
    p_pkg.add_argument("name", help="Package search term")

    sub.add_parser("list_apps", help="List installed/launchable apps as JSON")

    p_launch = sub.add_parser("launch_app", help="Launch an app by alias, package, or component")
    p_launch.add_argument("app", help="App alias, package, or full component")
    p_launch.add_argument("--url", default=None, help="Optional URL when launching Chrome")

    sub.add_parser("check", help="Validate ADB, ADBKeyboard, Cerebras key, and Ollama")

    p_shot = sub.add_parser("save_screenshot", help="Save screenshot to Gallery")
    p_shot.add_argument("--path", default="/sdcard/DCIM/Screenshots/wrangle.png")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    ensure_connected()

    if args.command == "check":
        return run_check()

    if args.command == "get_state":
        print_json(get_phone_state(task=args.task))
        return 0

    if args.command == "do_action":
        try:
            action = json.loads(args.action_json)
        except Exception as e:
            print_json({"ok": False, "error": f"invalid json: {e}"})
            return 2
        print_json(perform_action(action))
        return 0

    if args.command == "run":
        run(args.task, app=args.app, url=args.url, max_steps=args.max_steps)
        return 0

    if args.command == "read_screen":
        read_screen()
        return 0

    if args.command == "find_package":
        print_json(find_package(args.name))
        return 0

    if args.command == "list_apps":
        print_json(list_apps())
        return 0

    if args.command == "launch_app":
        launch_app(args.app, url=args.url)
        print_json({"ok": True, "executed": "launch", "app": args.app, "url": args.url})
        return 0

    if args.command == "save_screenshot":
        screenshot_and_save(args.path)
        return 0

    parser.print_help()
    return 1

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    raise SystemExit(cli())
