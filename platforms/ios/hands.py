"""
hands_impl/ios/ios_hands.py

iOS Hands -- executes actions via:
  - Apple Shortcuts URL schemes  (shortcuts://run-shortcut?name=...)
  - a-Shell commands             (if running inside a-Shell)
  - Pythonista appex APIs        (if running inside Pythonista)
  - clipboard via pasteboard

iOS is sandboxed so direct system access is limited compared to Android/desktop.
The strategy is: use Shortcuts for anything that needs OS-level access,
and handle the rest in-process via Python stdlib or app-specific APIs.

To run the Firewall on iOS, use a-Shell or Pythonista as the host.
"""
import subprocess
import urllib.parse
import urllib.request
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[2]))
from pathlib import Path



from core.hands.base import ActionResult

# Detect which iOS Python environment we're in
_IN_ASHELL     = os.path.exists("/usr/bin/python3") and "a-shell" in sys.executable.lower()
_IN_PYTHONISTA = "Pythonista" in sys.executable

try:
    import clipboard   # Pythonista built-in
    _HAS_CLIPBOARD = True
except ImportError:
    _HAS_CLIPBOARD = False


class IOSHands(BaseHands):

    @property
    def platform_id(self) -> str:
        return "ios"

    def list_capabilities(self) -> list[Capability]:
        caps = [
            Capability("clipboard_get",    "Read clipboard contents",          "ios", {"type": "clipboard_get",    "params": {}}),
            Capability("clipboard_set",    "Write text to clipboard",          "ios", {"type": "clipboard_set",    "params": {"text": ""}}),
            Capability("run_shortcut",     "Run an Apple Shortcut by name",    "ios", {"type": "run_shortcut",     "params": {"name": "", "input": ""}}),
            Capability("open_url",         "Open a URL in Safari",             "ios", {"type": "open_url",         "params": {"url": ""}}),
            Capability("run_command",      "Run a shell command (a-Shell)",    "ios", {"type": "run_command",      "params": {"cmd": ""}}),
            Capability("list_dir",         "List files in a directory",        "ios", {"type": "list_dir",         "params": {"path": "~"}}),
            Capability("notify",           "Show a notification via Shortcut", "ios", {"type": "notify",           "params": {"title": "", "body": ""}}),
        ]
        return caps

    def can_execute(self, action: dict) -> bool:
        supported = {c.name for c in self.list_capabilities()}
        return action.get("type") in supported

    def execute(self, action: dict) -> ActionResult:
        atype  = action.get("type", "")
        params = action.get("params", {})

        try:
            if atype == "clipboard_get":
                return self._clipboard_get()

            elif atype == "clipboard_set":
                return self._clipboard_set(params.get("text", ""))

            elif atype == "run_shortcut":
                return self._run_shortcut(
                    params.get("name", ""),
                    params.get("input", ""),
                )

            elif atype == "open_url":
                return self._open_url(params.get("url", ""))

            elif atype == "run_command":
                return self._shell(params.get("cmd", ""))

            elif atype == "list_dir":
                path = Path(params.get("path", "~")).expanduser()
                items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
                output = "\n".join(
                    ("📁 " if p.is_dir() else "📄 ") + p.name for p in items
                )
                return ActionResult(success=True, output=output)

            elif atype == "notify":
                # Delegate to a Shortcut named "TF Notify" that accepts
                # a JSON dict with title/body as input
                return self._run_shortcut(
                    "TF Notify",
                    f"{params.get('title','')}: {params.get('body','')}",
                )

            else:
                return ActionResult(success=False, error=f"Unknown action: {atype}")

        except Exception as e:
            return ActionResult(success=False, error=str(e))

    # ── Clipboard ─────────────────────────────────────────────────────────────

    def _clipboard_get(self) -> ActionResult:
        if _HAS_CLIPBOARD:
            import clipboard
            return ActionResult(success=True, output=clipboard.get())
        # a-Shell: pbpaste is available
        return self._shell("pbpaste")

    def _clipboard_set(self, text: str) -> ActionResult:
        if _HAS_CLIPBOARD:
            import clipboard
            clipboard.set(text)
            return ActionResult(success=True, output="Clipboard set.")
        return self._shell(f"echo '{text}' | pbcopy")

    # ── Shortcuts URL scheme ──────────────────────────────────────────────────

    def _run_shortcut(self, name: str, input_text: str = "") -> ActionResult:
        """
        Triggers an Apple Shortcut via URL scheme.
        Works from a-Shell with the 'open' command or Pythonista with webbrowser.
        The Shortcut must exist in the Shortcuts app on the device.
        """
        encoded_name  = urllib.parse.quote(name)
        encoded_input = urllib.parse.quote(input_text)
        url = f"shortcuts://run-shortcut?name={encoded_name}&input=text&text={encoded_input}"
        return self._open_url(url)

    def _open_url(self, url: str) -> ActionResult:
        if _IN_ASHELL:
            result = self._shell(f"open '{url}'")
            return result
        if _IN_PYTHONISTA:
            try:
                import webbrowser
                webbrowser.open(url)
                return ActionResult(success=True, output=f"Opened: {url}")
            except Exception as e:
                return ActionResult(success=False, error=str(e))
        # Fallback -- try subprocess open
        return self._shell(f"open '{url}'")

    # ── Shell (a-Shell only) ──────────────────────────────────────────────────

    def _shell(self, cmd: str) -> ActionResult:
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            return ActionResult(
                success=proc.returncode == 0,
                output=proc.stdout.strip() or proc.stderr.strip(),
                error=proc.stderr.strip() if proc.returncode != 0 else None,
            )
        except subprocess.TimeoutExpired:
            return ActionResult(success=False, error="Command timed out.")
        except Exception as e:
            return ActionResult(success=False, error=str(e))
