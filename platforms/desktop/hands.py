"""
platforms/desktop/hands.py -- Linux/macOS/Windows execution layer.
"""
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[2]))
import core.config as cfg
from core.hands.base import ActionResult

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
        return action.get("type") in {c["name"] for c in self.capabilities()}

    def execute(self, action):
        atype  = action.get("type","")
        params = action.get("params",{})
        try:
            if atype == "run_command":
                r = subprocess.run(params.get("cmd",""), shell=True, capture_output=True, text=True, timeout=30)
                return ActionResult(r.returncode==0, r.stdout.strip(), r.stderr.strip())

            elif atype == "clipboard_get":
                cmds = {"macos":["pbpaste"],"linux":["xclip","-selection","clipboard","-o"],
                        "windows":["powershell","-command","Get-Clipboard"]}
                r = subprocess.run(cmds.get(cfg.PLATFORM,["xclip","-o"]), capture_output=True, text=True)
                return ActionResult(True, r.stdout.strip())

            elif atype == "clipboard_set":
                text = params.get("text","")
                if cfg.PLATFORM == "macos":
                    subprocess.run(["pbcopy"], input=text, text=True)
                elif cfg.PLATFORM == "linux":
                    subprocess.run(["xclip","-selection","clipboard"], input=text, text=True)
                else:
                    subprocess.run(["powershell","-command",f"Set-Clipboard '{text}'"])
                return ActionResult(True, "Clipboard set.")

            elif atype == "open_app":
                app = params.get("app_name", params.get("package",""))
                if cfg.PLATFORM == "macos":
                    r = subprocess.run(["open","-a",app], capture_output=True, text=True)
                elif cfg.PLATFORM == "linux":
                    r = subprocess.run([app], capture_output=True, text=True)
                else:
                    r = subprocess.run(["start",app], shell=True, capture_output=True, text=True)
                return ActionResult(r.returncode==0, f"Opened {app}", r.stderr.strip())

            return ActionResult(False, error=f"Unknown: {atype}")
        except Exception as e:
            return ActionResult(False, error=str(e))
