"""
platforms/rish_phone/hands.py -- Android device control over SSH + rish,
no ADB/USB required. rish gives adb-shell-equivalent access on-device via
Shizuku; we reach it over the existing durable SSH tunnel to the phone.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))
from core.hands.base import ActionResult

SSH_PHONE_CMD = [
    "ssh", "-tt", "-p", "8022", "-i", str(Path.home() / ".ssh/id_ed25519_phone"),
    "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", "u0_a337@100.75.171.40",
]

_KEY_EVENTS = {
    "home": "3", "back": "4", "recent": "187", "enter": "66",
    "volume_up": "24", "volume_down": "25",
}

_ACTIONS = {
    "tap", "swipe", "key_event", "type_text", "scroll_down", "scroll_up",
    "screenshot_adb", "dump_ui", "get_screen", "vibrate", "torch",
    "battery_status", "wifi_info", "wifi_scan", "location",
    "clipboard_get", "clipboard_set", "open_app", "close_app",
    "find_and_tap", "find_and_type", "take_photo", "send_sms",
    "toast", "notify",
}


class RishPhoneHands:
    @property
    def platform_id(self):
        return "android-rish"

    def capabilities(self):
        return [{"name": a, "description": f"Phone: {a}"} for a in sorted(_ACTIONS)]

    def can_execute(self, action):
        return action.get("type", "") in _ACTIONS

    def _rish(self, cmd, timeout=25):
        # rish needs -c "cmd" (like sh -c), RISH_APPLICATION_ID exported (only
        # lives in .bashrc, which non-interactive SSH won't source), and a
        # forced pseudo-tty (-tt on the ssh command) or it silently no-ops.
        remote = f'export RISH_APPLICATION_ID=com.termux; ~/rish/rish -c "{cmd}"'
        try:
            r = subprocess.run(SSH_PHONE_CMD + [remote],
                                capture_output=True, text=True, timeout=timeout)
            return ActionResult(r.returncode == 0, r.stdout.strip(), r.stderr.strip())
        except subprocess.TimeoutExpired:
            return ActionResult(False, error=f"timeout after {timeout}s")
        except Exception as e:
            return ActionResult(False, error=str(e))

    def execute(self, action):
        atype = action.get("type", "")
        p = action.get("params", {})
        try:
            if atype == "tap":
                return self._rish(f"input tap {p['x']} {p['y']}")
            if atype == "swipe":
                dur = p.get("duration_ms", 300)
                return self._rish(f"input swipe {p['x1']} {p['y1']} {p['x2']} {p['y2']} {dur}")
            if atype == "key_event":
                code = _KEY_EVENTS.get(p.get("key", ""), p.get("key", ""))
                return self._rish(f"input keyevent {code}")
            if atype == "type_text":
                text = p.get("text", "").replace(" ", "%s").replace("'", "")
                return self._rish(f"input text '{text}'")
            if atype == "scroll_down":
                return self._rish("input swipe 500 1400 500 400 300")
            if atype == "scroll_up":
                return self._rish("input swipe 500 400 500 1400 300")
            if atype in ("screenshot_adb", "get_screen"):
                path = p.get("path", "/sdcard/tf_screenshot.png")
                r = self._rish(f"screencap -p {path}")
                if r.success:
                    r.output = f"Screenshot saved on phone at {path}"
                return r
            if atype == "dump_ui":
                return self._rish("uiautomator dump /sdcard/tf_ui.xml && cat /sdcard/tf_ui.xml")
            if atype == "toast":
                r = subprocess.run(["/usr/local/bin/termux-toast", p.get("message", "")],
                                    capture_output=True, text=True, timeout=20)
                return ActionResult(r.returncode == 0, "toast sent" if r.returncode == 0 else "", r.stderr.strip())
            if atype == "notify":
                r = subprocess.run(
                    ["/usr/local/bin/termux-notification", "--title", p.get("title", ""), "--content", p.get("content", "")],
                    capture_output=True, text=True, timeout=20)
                return ActionResult(r.returncode == 0, "notification sent" if r.returncode == 0 else "", r.stderr.strip())
            if atype == "vibrate":
                dur = p.get("duration_ms", 500)
                return self._rish(f"cmd vibrator vibrate {dur}")
            if atype == "battery_status":
                r = self._rish("dumpsys battery")
                if r.success:
                    import re
                    m = re.search(r"^\s*level:\s*(\d+)", r.output, re.MULTILINE)
                    r.output = f"Battery: {m.group(1)}%" if m else r.output[:200]
                return r
            if atype == "wifi_info":
                r = self._rish("dumpsys wifi")
                if r.success:
                    import re
                    m = re.search(r"mWifiInfo[^\n]*", r.output)
                    r.output = m.group(0) if m else r.output[:300]
                return r
            if atype == "clipboard_get":
                return self._rish("cmd clipboard get-primary-clip 2>/dev/null || service call clipboard 2")
            if atype == "clipboard_set":
                text = p.get("text", "").replace("'", "")
                return self._rish(f"cmd clipboard set-primary-clip '{text}'")
            if atype == "open_app":
                pkg = p.get("package", "")
                return self._rish(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
            if atype == "close_app":
                pkg = p.get("package", "")
                return self._rish(f"am force-stop {pkg}")
            return ActionResult(False, error=f"not implemented on phone: {atype}")
        except Exception as e:
            return ActionResult(False, error=str(e))
