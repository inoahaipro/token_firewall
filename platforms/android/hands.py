"""
platforms/android/hands.py -- Android device execution layer.

Combines Termux API (device sensors/APIs) and ADB (UI automation/system settings).
Auto-detects what's available. Gracefully degrades if one is missing.
"""
import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import sys

sys.path.insert(0, str(Path(__file__).parents[2]))
import core.config as cfg
from core.hands.base import ActionResult


# ── Android key codes ─────────────────────────────────────────────────────────

_KEY = {
    "home":       3,  "back":       4,  "menu":      82,
    "power":      26, "volume_up":  24, "volume_down":25,
    "enter":      66, "delete":     67, "tab":       61,
    "search":     84, "recent":     187,"camera":    27,
}


# ── Capability sets ───────────────────────────────────────────────────────────

_TERMUX_ACTIONS = {
    "battery_status", "wifi_info", "wifi_scan", "send_sms", "set_volume",
    "torch", "clipboard_get", "clipboard_set", "vibrate", "take_photo",
    "location", "run_command",
}

_ADB_ACTIONS = {
    "tap", "long_press", "swipe", "type_text", "key_event", "open_app",
    "close_app", "get_screen", "dump_ui", "screenshot_adb", "scroll_down",
    "scroll_up", "get_current_app", "install_apk", "adb_command", "wait",
    "find_and_tap", "find_and_type", "find_and_scroll",
}


# ── Main Android hands ────────────────────────────────────────────────────────

class AndroidHands:

    def __init__(self):
        self._termux_ok  = self._check_termux()
        self._adb_ok     = self._check_adb()
        self._screen_w   = None
        self._screen_h   = None

    @property
    def platform_id(self) -> str:
        return "android"

    def can_execute(self, action: dict) -> bool:
        atype = action.get("type", "")
        if atype in cfg.DISABLED_ACTIONS:
            return False
        if atype in _TERMUX_ACTIONS and self._termux_ok and not cfg.DISABLE_TERMUX:
            return True
        if atype in _ADB_ACTIONS and self._adb_ok and not cfg.DISABLE_ADB:
            return True
        return False

    def capabilities(self) -> list[dict]:
        caps = []
        if self._termux_ok and not cfg.DISABLE_TERMUX:
            caps += [
                {"name": "battery_status", "description": "Get battery level and status"},
                {"name": "wifi_info",      "description": "Get WiFi connection details"},
                {"name": "wifi_scan",      "description": "Scan for nearby WiFi networks"},
                {"name": "torch",          "description": "Toggle flashlight"},
                {"name": "vibrate",        "description": "Vibrate device"},
                {"name": "take_photo",     "description": "Take a photo"},
                {"name": "location",       "description": "Get GPS location"},
                {"name": "clipboard_get",  "description": "Read clipboard"},
                {"name": "clipboard_set",  "description": "Write clipboard"},
                {"name": "send_sms",       "description": "Send SMS"},
            ]
        if self._adb_ok and not cfg.DISABLE_ADB:
            caps += [
                {"name": "tap",            "description": "Tap screen at x,y"},
                {"name": "swipe",          "description": "Swipe gesture"},
                {"name": "scroll_down",    "description": "Scroll down"},
                {"name": "scroll_up",      "description": "Scroll up"},
                {"name": "type_text",      "description": "Type text"},
                {"name": "key_event",      "description": "Press a key"},
                {"name": "open_app",       "description": "Launch an app"},
                {"name": "screenshot_adb", "description": "Take screenshot"},
                {"name": "adb_command",    "description": "Run adb shell command"},
                {"name": "find_and_tap",   "description": "Find UI element and tap it"},
                {"name": "find_and_type",  "description": "Find input field and type"},
            ]
        return caps

    def execute(self, action: dict) -> ActionResult:
        atype = action.get("type", "")
        if atype in cfg.DISABLED_ACTIONS:
            return ActionResult(success=False, error=f"Action '{atype}' is disabled.")

        try:
            if atype in _TERMUX_ACTIONS and self._termux_ok and not cfg.DISABLE_TERMUX:
                return self._termux(action)
            if atype in _ADB_ACTIONS and self._adb_ok and not cfg.DISABLE_ADB:
                return self._adb_action(action)
            return ActionResult(success=False, error=f"No handler for action type: {atype}")
        except Exception as e:
            return ActionResult(success=False, error=f"Exception: {e}")

    # ── Termux layer ──────────────────────────────────────────────────────────

    def _termux(self, action: dict) -> ActionResult:
        atype  = action.get("type", "")
        params = action.get("params", {})

        if atype == "battery_status":
            r = self._run(["termux-battery-status"])
            return self._json_result(r, self._fmt_battery)

        elif atype == "wifi_info":
            r = self._run(["termux-wifi-connectioninfo"])
            return self._json_result(r, self._fmt_wifi)

        elif atype == "wifi_scan":
            r = self._run(["termux-wifi-scaninfo"])
            return self._json_result(r)

        elif atype == "send_sms":
            return self._run(["termux-sms-send", "-n", params.get("number",""), params.get("message","")])

        elif atype == "set_volume":
            return self._run(["termux-volume", params.get("stream","media"), str(params.get("level",50))])

        elif atype == "torch":
            return self._run(["termux-torch", params.get("state","off")])

        elif atype == "clipboard_get":
            return self._run(["termux-clipboard-get"])

        elif atype == "clipboard_set":
            proc = subprocess.run(["termux-clipboard-set"], input=params.get("text",""),
                                   capture_output=True, text=True)
            return ActionResult(success=proc.returncode==0, output="Clipboard updated.")

        elif atype == "vibrate":
            return self._run(["termux-vibrate", "-d", str(params.get("duration_ms",500))])

        elif atype == "take_photo":
            fname = params.get("filename", "/sdcard/photo.jpg")
            r = self._run(["termux-camera-photo", fname])
            if r.success:
                # Trigger gallery scan so photo appears immediately
                subprocess.run(
                    ["adb","shell","am","broadcast",
                     "-a","android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                     "-d",f"file://{fname}"],
                    capture_output=True, timeout=10
                )
            return r

        elif atype == "location":
            r = self._run(["termux-location", "-p", params.get("provider","gps"), "-r","once"])
            return self._json_result(r)

        elif atype == "run_command":
            cmd = params.get("cmd","echo ok")
            return self._run(cmd.split() if isinstance(cmd, str) else cmd)

        return ActionResult(success=False, error=f"Unknown Termux action: {atype}")

    def _run(self, cmd: list) -> ActionResult:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return ActionResult(success=False, error=proc.stderr.strip() or f"Exit {proc.returncode}")
        return ActionResult(success=True, output=proc.stdout.strip())

    def _json_result(self, r: ActionResult, formatter=None) -> ActionResult:
        if not r.success:
            return r
        try:
            d = json.loads(r.output)
            r.output = formatter(d) if formatter else json.dumps(d, indent=2)
        except Exception:
            pass
        return r

    def _fmt_battery(self, d: dict) -> str:
        pct    = d.get("percentage", "?")
        status = d.get("status","unknown").replace("_"," ").title()
        health = d.get("health","?").title()
        temp   = d.get("temperature","?")
        plugged= d.get("plugged","").replace("PLUGGED_","").title()
        return f"Battery: {pct}% -- {status}{' (' + plugged + ')' if plugged else ''}\nHealth: {health}, Temp: {temp}°C"

    def _fmt_wifi(self, d: dict) -> str:
        ssid  = d.get("ssid","unknown")
        ip    = d.get("ip","unknown")
        speed = d.get("link_speed_mbps","?")
        return f"WiFi: {ssid} · IP: {ip} · {speed} Mbps"

    # ── ADB layer ─────────────────────────────────────────────────────────────

    def _adb_action(self, action: dict) -> ActionResult:
        atype  = action.get("type","")
        params = action.get("params",{})

        # Verify device is ready
        ok, err = self._check_device()
        if not ok:
            return ActionResult(success=False, error=err)

        if atype == "tap":
            x, y = self._clamp(params.get("x",500), params.get("y",500))
            return self._adb(f"shell input tap {x} {y}")

        elif atype == "long_press":
            x, y = self._clamp(params.get("x",500), params.get("y",500))
            dur   = params.get("duration_ms",1000)
            return self._adb(f"shell input swipe {x} {y} {x} {y} {dur}")

        elif atype == "swipe":
            x1,y1 = self._clamp(params.get("x1",500), params.get("y1",1400))
            x2,y2 = self._clamp(params.get("x2",500), params.get("y2",400))
            dur    = params.get("duration_ms",300)
            return self._adb(f"shell input swipe {x1} {y1} {x2} {y2} {dur}")

        elif atype == "type_text":
            text = params.get("text","")
            if not text:
                return ActionResult(success=False, error="No text provided")
            esc = text.replace(" ","%s").replace("'","\\'").replace('"','\\"').replace("&","\\&")
            r = self._adb(f"shell input text '{esc}'")
            return r if r.success else ActionResult(success=False, error=r.error)

        elif atype == "key_event":
            key  = params.get("key","home")
            code = _KEY.get(key.lower(), key)
            return self._adb(f"shell input keyevent {code}")

        elif atype == "open_app":
            return self._open_app(params)

        elif atype == "close_app":
            pkg = params.get("package","")
            return self._adb(f"shell am force-stop {pkg}")

        elif atype == "get_screen":
            return self._screen_size()

        elif atype == "dump_ui":
            r = self._adb("shell uiautomator dump /sdcard/ui_dump.xml")
            if not r.success:
                return r
            return self._adb("shell cat /sdcard/ui_dump.xml")

        elif atype == "screenshot_adb":
            path = params.get("path","/sdcard/screenshot.png")
            return self._adb(f"shell screencap -p {path}")

        elif atype == "scroll_down":
            w, h = self._dims()
            x     = params.get("x", w//2)
            steps = params.get("steps", 5)
            return self._adb(f"shell input swipe {x} {int(h*0.7)} {x} {int(h*0.3)} {300*steps}")

        elif atype == "scroll_up":
            w, h = self._dims()
            x     = params.get("x", w//2)
            steps = params.get("steps", 5)
            return self._adb(f"shell input swipe {x} {int(h*0.3)} {x} {int(h*0.7)} {300*steps}")

        elif atype == "get_current_app":
            return self._adb("shell dumpsys activity activities | grep mResumedActivity | head -1")

        elif atype == "install_apk":
            return self._adb(f"install -r {params.get('path','')}")

        elif atype == "adb_command":
            cmd = params.get("cmd","").strip()
            if not cmd:
                return ActionResult(success=False, error="No command provided")
            if cmd.startswith("shell "):
                cmd = cmd[6:]
            r = self._adb(f"shell {cmd}")
            out = (r.output or "").strip()
            return ActionResult(success=r.success, output=out or "Done", error=r.error)

        elif atype == "wait":
            secs = float(params.get("seconds",1))
            time.sleep(min(secs, 10))
            return ActionResult(success=True, output=f"Waited {secs}s")

        elif atype == "find_and_tap":
            text    = params.get("text","")
            res_id  = params.get("id","")
            desc    = params.get("desc","")
            fuzzy   = params.get("fuzzy",True)
            node = self._find_node(text=text, res_id=res_id, desc=desc, fuzzy=fuzzy)
            if not node:
                return ActionResult(success=False, error=f"Element not found: {text or res_id or desc}")
            x, y = node
            r = self._adb(f"shell input tap {x} {y}")
            if r.success:
                return ActionResult(success=True, output=f"Tapped '{text or res_id or desc}' at ({x},{y})")
            return r

        elif atype == "find_and_type":
            node = self._find_node(
                text=params.get("text",""),
                res_id=params.get("id",""),
                fuzzy=params.get("fuzzy",True),
            )
            if node:
                self._adb(f"shell input tap {node[0]} {node[1]}")
                time.sleep(0.3)
            content = params.get("content","")
            esc = content.replace(" ","%s").replace("'","\\'")
            return self._adb(f"shell input text '{esc}'")

        elif atype == "find_and_scroll":
            direction = params.get("direction","down")
            target    = params.get("text","")
            for _ in range(int(params.get("max_swipes",5))):
                if target and self._find_node(text=target):
                    return ActionResult(success=True, output=f"Found '{target}'")
                w, h = self._dims()
                x = w // 2
                if direction == "down":
                    self._adb(f"shell input swipe {x} {int(h*0.7)} {x} {int(h*0.3)} 300")
                else:
                    self._adb(f"shell input swipe {x} {int(h*0.3)} {x} {int(h*0.7)} 300")
                time.sleep(0.5)
            return ActionResult(success=True, output="Scrolled")

        return ActionResult(success=False, error=f"Unknown ADB action: {atype}")

    def _open_app(self, params: dict) -> ActionResult:
        pkg      = params.get("package","")
        url      = params.get("url","")
        app_name = params.get("app_name","")

        if url:
            return self._adb(f"shell am start -a android.intent.action.VIEW -d '{url}'")

        # Resolve friendly name to package if needed
        if pkg and "." not in pkg:
            try:
                from platforms.android.resolver import AppResolver
                r = AppResolver(); r.resolve()
                resolved = r.resolve_unknown(pkg)
                if resolved:
                    pkg = resolved
            except Exception:
                pass

        if not pkg:
            return ActionResult(success=False, error="No package name provided")

        display = app_name or pkg.split(".")[-1].replace("_"," ").title()

        # Try resolve-activity for exact component
        component = None
        res = self._adb(f"shell cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {pkg}")
        if res.success:
            for line in res.output.strip().splitlines():
                line = line.strip()
                if "/" in line and not line.startswith(("priority","preferredOrder")):
                    component = line
                    break

        if component:
            r = self._adb(f"shell am start -n {component}")
            out = (r.output or "").lower()
            if r.success and "error" not in out and "unable" not in out:
                return ActionResult(success=True, output=f"Opened {display}")

        # Fallback: MAIN/LAUNCHER intent
        r2 = self._adb(f"shell am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {pkg}")
        out2 = (r2.output or "").lower()
        if r2.success and "error" not in out2 and "unable" not in out2:
            return ActionResult(success=True, output=f"Opened {display}")

        # Last resort: monkey
        r3 = self._adb(f"shell monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
        if r3.success:
            return ActionResult(success=True, output=f"Opened {display}")

        return ActionResult(success=False, error=f"Could not open {pkg}")

    def _find_node(self, text="", res_id="", desc="", fuzzy=True) -> Optional[tuple]:
        """Find UI element. Tries uiautomator2 first, falls back to XML dump."""
        # Try uiautomator2 first (faster, more reliable, works on WebViews)
        try:
            from platforms.android import u2
            result = u2.find_element(text=text, res_id=res_id, desc=desc, fuzzy=fuzzy)
            if result:
                return result
        except Exception:
            pass
        # Fallback: raw XML dump
        try:
            self._adb("shell uiautomator dump /sdcard/ui_dump.xml")
            time.sleep(0.3)
            r = self._adb("shell cat /sdcard/ui_dump.xml")
            if not r.success or not r.output:
                return None
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(r.output)
            except Exception:
                return None
            search = (text or res_id or desc).lower()
            if not search:
                return None

            def matches(node):
                for attr in ("text","content-desc","resource-id"):
                    v = node.get(attr,"").lower()
                    if v and (search in v if fuzzy else v == search):
                        return True
                return False

            def center(node):
                m = re.findall(r"\d+", node.get("bounds",""))
                if len(m) == 4:
                    x1,y1,x2,y2 = map(int,m)
                    return (x1+x2)//2, (y1+y2)//2
                return None

            for node in root.iter():
                if matches(node):
                    c = center(node)
                    if c:
                        return c
        except Exception as e:
            print(f"[UI] find error: {e}")
        return None

    # ── ADB helpers ───────────────────────────────────────────────────────────

    def _adb(self, cmd: str) -> ActionResult:
        try:
            proc = subprocess.run(
                ["adb"] + shlex.split(cmd),
                capture_output=True, text=True, timeout=30,
            )
            out = proc.stdout.strip()
            err = proc.stderr.strip()
            if proc.returncode != 0 or (err and "error" in err.lower()):
                return ActionResult(success=False, output=out, error=err or f"Exit {proc.returncode}")
            return ActionResult(success=True, output=out or "OK")
        except subprocess.TimeoutExpired:
            return ActionResult(success=False, error="ADB timed out")
        except FileNotFoundError:
            return ActionResult(success=False, error="adb not found -- run: pkg install android-tools")
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    def _check_device(self) -> tuple[bool, str]:
        try:
            r = subprocess.run(["adb","devices"], capture_output=True, text=True, timeout=5)
            lines = r.stdout.strip().splitlines()
            if any(l.endswith("device") for l in lines[1:]):
                return True, ""
            probe = subprocess.run(["adb","shell","echo","ok"], capture_output=True, text=True, timeout=5)
            if probe.returncode == 0 and "ok" in probe.stdout:
                return True, ""
            return False, "No ADB device. Enable USB/Wireless debugging."
        except FileNotFoundError:
            return False, "adb not found -- run: pkg install android-tools"
        except Exception as e:
            return False, str(e)

    def _check_termux(self) -> bool:
        try:
            r = subprocess.run(["termux-battery-status"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def _check_adb(self) -> bool:
        ok, _ = self._check_device()
        return ok

    def _screen_size(self) -> ActionResult:
        r = self._adb("shell wm size")
        if r.success and "Physical size:" in r.output:
            try:
                dims = r.output.split("Physical size:")[1].strip()
                w, h = dims.split("x")
                self._screen_w, self._screen_h = int(w), int(h)
                return ActionResult(success=True, output=f"{w}x{h}")
            except Exception:
                pass
        return ActionResult(success=False, error="Could not parse screen size")

    def _dims(self) -> tuple[int,int]:
        if self._screen_w and self._screen_h:
            return self._screen_w, self._screen_h
        self._screen_size()
        return self._screen_w or 1080, self._screen_h or 1920

    def _clamp(self, x: int, y: int) -> tuple[int,int]:
        w, h = self._dims()
        return max(0, min(int(x), w-1)), max(0, min(int(y), h-1))


    # ── Action verify loop ────────────────────────────────────────────────────

    def execute_and_verify(self, action: dict, verify_fn=None, max_retries: int = 2) -> ActionResult:
        """
        Execute an action, optionally verify it succeeded by checking screen state.
        verify_fn: callable that takes the UI XML dump and returns True if action succeeded.
        Retries up to max_retries times if verification fails.
        """
        for attempt in range(max_retries + 1):
            result = self.execute(action)
            if not result.success:
                return result
            if verify_fn is None:
                return result
            # Wait for UI to settle
            time.sleep(0.5)
            # Dump current UI and verify
            dump_r = self._adb("shell uiautomator dump /sdcard/ui_verify.xml")
            if dump_r.success:
                xml_r = self._adb("shell cat /sdcard/ui_verify.xml")
                if xml_r.success and verify_fn(xml_r.output or ""):
                    return result
            if attempt < max_retries:
                print(f"[VERIFY] attempt {attempt+1} failed, retrying...")
                time.sleep(0.5)
        return result  # Return last result even if verify failed
