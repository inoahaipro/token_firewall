"""
platforms/desktop/hands.py -- Linux/macOS/Windows execution layer.
"""
import re
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[2]))
import core.config as cfg
from core.hands.base import ActionResult

# Reverse-DNS Android package id (e.g. com.spotify.music) -- CompositeHands
# tries DesktopHands before RishPhoneHands, so without this check an
# open_app resolved against the PHONE's package list (by
# platforms/android/resolver.py) got claimed here first and DesktopHands
# tried to exec a literal binary called "Spotify" on the PC, which doesn't
# exist. Found live right after fixing the resolver itself -- same
# claim-ordering bug as the clipboard one, different action type.
_ANDROID_PKG_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+){2,}$")

class DesktopHands:
    @property
    def platform_id(self): return cfg.PLATFORM

    def capabilities(self):
        return [
            {"name":"run_command",   "description":"Run a shell command"},
            {"name":"clipboard_get", "description":"Read clipboard"},
            {"name":"clipboard_set", "description":"Write clipboard"},
            {"name":"open_app",      "description":"Open an application"},
            {"name":"screenshot_adb","description":"Take a screenshot"},
        ]

    def can_execute(self, action):
        if action.get("type") == "open_app":
            pkg = action.get("params", {}).get("package", "")
            if _ANDROID_PKG_RE.match(pkg):
                return False  # phone package -- let RishPhoneHands claim it
        return action.get("type") in {c["name"] for c in self.capabilities()}

    def execute(self, action):
        atype  = action.get("type","")
        params = action.get("params",{})
        try:
            if atype == "run_command":
                r = subprocess.run(params.get("cmd",""), shell=True, capture_output=True, text=True, timeout=30)
                return ActionResult(r.returncode==0, r.stdout.strip(), r.stderr.strip())

            elif atype == "clipboard_get":
                # CompositeHands tries DesktopHands before RishPhoneHands, so
                # a bare "clipboard" request silently reads THIS machine's
                # clipboard, not the phone's -- found live: this returned a
                # stale PC clipboard value with zero indication it wasn't the
                # phone's, which a caller could easily relay as ground truth
                # for the wrong device. Label the source so that can't happen
                # silently; a real per-device clipboard action is the actual
                # fix but that's a routing/product decision, not a one-liner.
                cmds = {"macos":["pbpaste"],"linux":["xclip","-selection","clipboard","-o"],
                        "windows":["powershell","-command","Get-Clipboard"]}
                r = subprocess.run(cmds.get(cfg.PLATFORM,["xclip","-o"]), capture_output=True, text=True)
                if r.returncode != 0:
                    # xclip exits non-zero when the clipboard is empty or holds
                    # non-text data (e.g. "target STRING not available") -- that's
                    # a normal, valid state, not a real failure.
                    if "target string not available" in r.stderr.lower():
                        return ActionResult(True, "(PC clipboard is empty)")
                    return ActionResult(False, error=r.stderr.strip() or "clipboard read failed")
                return ActionResult(True, f"[PC clipboard] {r.stdout.strip() or '(empty)'}")

            elif atype == "clipboard_set":
                text = params.get("text","")
                if cfg.PLATFORM == "macos":
                    subprocess.run(["pbcopy"], input=text, text=True)
                elif cfg.PLATFORM == "linux":
                    subprocess.run(["xclip","-selection","clipboard"], input=text, text=True)
                else:
                    # PowerShell single-quote strings escape an embedded '
                    # by doubling it, not by stripping -- naive interpolation
                    # here was the same class of injection bug fixed in
                    # rish_phone/hands.py, just for the Windows path.
                    ps_safe = text.replace("'", "''")
                    subprocess.run(["powershell","-command",f"Set-Clipboard '{ps_safe}'"])
                return ActionResult(True, "PC clipboard set.")

            elif atype == "open_app":
                app = params.get("app_name", params.get("package",""))
                if cfg.PLATFORM == "macos":
                    r = subprocess.run(["open","-a",app], capture_output=True, text=True)
                elif cfg.PLATFORM == "linux":
                    r = subprocess.run([app], capture_output=True, text=True)
                else:
                    # No shell=True -- avoids cmd.exe interpreting metacharacters
                    # in app. "start" needs an explicit empty title arg on Windows.
                    r = subprocess.run(["cmd","/c","start","",app], capture_output=True, text=True)
                return ActionResult(r.returncode==0, f"Opened {app}", r.stderr.strip())

            return ActionResult(False, error=f"Unknown: {atype}")
        except Exception as e:
            return ActionResult(False, error=str(e))
