"""
platforms/rish_phone/hands.py -- Android device control over SSH + rish,
no ADB/USB required. rish gives adb-shell-equivalent access on-device via
Shizuku; we reach it over the existing durable SSH tunnel to the phone.
"""
import shlex
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
        #
        # cmd itself is shlex.quote()'d as a single opaque argument for the
        # SSH/local-shell parse -- naive f-string double-quote wrapping here
        # let a literal " in user-supplied text (e.g. a type_text/toast/
        # clipboard message) close the outer quote early and inject
        # arbitrary shell commands on the phone, confirmed live with a real
        # (harmless) proof-of-concept before this fix. cmd's OWN embedded
        # values must separately be shlex.quote()'d by the caller for rish's
        # later internal sh -c interpretation -- see the per-action-type
        # quoting below.
        remote = f'export RISH_APPLICATION_ID=com.termux; ~/rish/rish -c {shlex.quote(cmd)}'
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
                x, y = int(p["x"]), int(p["y"])
                return self._rish(f"input tap {x} {y}")
            if atype == "swipe":
                x1, y1, x2, y2 = int(p["x1"]), int(p["y1"]), int(p["x2"]), int(p["y2"])
                dur = int(p.get("duration_ms", 300))
                return self._rish(f"input swipe {x1} {y1} {x2} {y2} {dur}")
            if atype == "key_event":
                raw_key = p.get("key", "")
                code = _KEY_EVENTS.get(raw_key, raw_key)
                if not str(code).isdigit():
                    return ActionResult(False, error=f"unrecognized key: {raw_key!r}")
                return self._rish(f"input keyevent {code}")
            if atype == "type_text":
                # shlex.quote (not the old blunt '.replace("'", "")') so the
                # text survives rish's own internal sh -c interpretation
                # as a fully literal argument, regardless of what characters
                # it contains -- fixes a real shell-injection bug found live.
                text = shlex.quote(p.get("text", "").replace(" ", "%s"))
                return self._rish(f"input text {text}")
            if atype == "scroll_down":
                return self._rish("input swipe 500 1400 500 400 300")
            if atype == "scroll_up":
                return self._rish("input swipe 500 400 500 1400 300")
            if atype in ("screenshot_adb", "get_screen"):
                path = p.get("path", "/sdcard/tf_screenshot.png")
                r = self._rish(f"screencap -p {shlex.quote(path)}")
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
                # `cmd vibrator vibrate` over rish/Shizuku hits "Can't find
                # service: vibrator" -- that binder-level service isn't
                # reachable from Shizuku's restricted context on this device.
                # termux-vibrate is a real Termux:API command that doesn't
                # need rish at all -- confirmed working directly over SSH.
                # Found live: this action was advertised as supported but
                # always failed.
                dur = int(p.get("duration_ms", 500))
                r = subprocess.run(SSH_PHONE_CMD + [f"termux-vibrate -d {dur}"],
                                    capture_output=True, text=True, timeout=15)
                return ActionResult(r.returncode == 0, "vibrated" if r.returncode == 0 else "", r.stderr.strip())
            if atype == "torch":
                # Same story as vibrate above -- no rish `cmd` equivalent was
                # ever implemented (fell through to the generic "not
                # implemented on phone" error), and termux-torch is a plain
                # Termux:API command that works without rish.
                state = p.get("state", "on").strip().lower()
                if state not in ("on", "off"):
                    state = "on"
                r = subprocess.run(SSH_PHONE_CMD + [f"termux-torch {state}"],
                                    capture_output=True, text=True, timeout=15)
                return ActionResult(r.returncode == 0, f"torch {state}" if r.returncode == 0 else "", r.stderr.strip())
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
                text = shlex.quote(p.get("text", ""))
                return self._rish(f"cmd clipboard set-primary-clip {text}")
            if atype == "open_app":
                pkg = shlex.quote(p.get("package", ""))
                r = self._rish(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
                # `monkey` (via rish) dumps its own noisy arg-parsing debug
                # preamble ("bash arg: -p\nbash arg: com.spotify.music...")
                # ahead of the real result -- that was leaking straight
                # through as the action's output instead of a clean
                # confirmation, inconsistent with every other action here
                # that formats its own result. Found live via independent
                # testing on another instance.
                if r.success:
                    r.output = "Done -- app opened."
                return r
            if atype == "close_app":
                pkg = shlex.quote(p.get("package", ""))
                return self._rish(f"am force-stop {pkg}")
            return ActionResult(False, error=f"not implemented on phone: {atype}")
        except Exception as e:
            return ActionResult(False, error=str(e))
