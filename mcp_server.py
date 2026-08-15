#!/usr/bin/env python3
"""
mcp_server.py -- Token Firewall MCP Server

Exposes Token Firewall device actions as MCP tools that OpenClaw agents
can call directly. Two transport modes:

  stdio (default):
    OpenClaw spawns this as a child process.
    No IP/port needed. Run via SKILL.md instructions.

  streamable-http (if firewall is already running):
    Add to mcporter.json:
    {"mcpServers": {"firewall": {"baseUrl": "http://127.0.0.1:8000/mcp"}}}

Usage:
    python mcp_server.py           # stdio mode (for OpenClaw)
    python mcp_server.py --http    # HTTP mode on port 8001
"""
import json
import sys
import os
import struct
import argparse
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))


# ── MCP Protocol (minimal JSON-RPC 2.0 over stdio) ───────────────────────────

class MCPServer:
    def __init__(self, name: str, version: str):
        self.name    = name
        self.version = version
        self._tools  = {}

    def tool(self, name: str, description: str, schema: dict):
        """Register a tool."""
        def decorator(fn):
            self._tools[name] = {
                "fn":          fn,
                "description": description,
                "inputSchema": schema,
            }
            return fn
        return decorator

    def run_stdio(self):
        """Run MCP server over stdin/stdout (JSON-RPC 2.0)."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue

            resp = self._handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

    def _handle(self, req: dict):
        method = req.get("method", "")
        rid    = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            return self._ok(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities":    {"tools": {}},
                "serverInfo":      {"name": self.name, "version": self.version},
            })

        elif method == "tools/list":
            tools = []
            for name, t in self._tools.items():
                tools.append({
                    "name":        name,
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                })
            return self._ok(rid, {"tools": tools})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args      = params.get("arguments", {})
            if tool_name not in self._tools:
                return self._err(rid, -32601, f"Tool not found: {tool_name}")
            try:
                result = self._tools[tool_name]["fn"](**args)
                return self._ok(rid, {
                    "content": [{"type": "text", "text": str(result)}],
                    "isError": False,
                })
            except Exception as e:
                return self._ok(rid, {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                })

        elif method == "notifications/initialized":
            return None  # no response needed

        return self._err(rid, -32601, f"Method not found: {method}")

    def _ok(self, rid, result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _err(self, rid, code, msg):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


# ── Tool implementations ──────────────────────────────────────────────────────

def _call_firewall(prompt: str) -> str:
    """Send a prompt to the running firewall server."""
    import urllib.request, urllib.error
    payload = json.dumps({
        "model":    "token-firewall",
        "messages": [{"role": "user", "content": prompt}],
        "stream":   False,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Firewall error: {e}"


def _exec_action(action: dict) -> str:
    """Execute a device action directly via firewall /tools/execute endpoint."""
    import urllib.request
    payload = json.dumps({"action": action}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/tools/execute",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("output", "Done")
    except Exception as e:
        return f"Error: {e}"


def _get_screen() -> str:
    """Get current screen state from firewall."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/v1/screen", timeout=10) as resp:
            return json.dumps(json.loads(resp.read()), indent=2)
    except Exception as e:
        return f"Error: {e}"


# ── Build and register tools ──────────────────────────────────────────────────

server = MCPServer("token-firewall", "3.0")


@server.tool(
    "device_command",
    "Send a natural language command to the device. Examples: 'open spotify', "
    "'check battery', 'set brightness to 50%', 'take a photo then go home'.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Natural language device command"},
        },
        "required": ["command"],
    },
)
def device_command(command: str) -> str:
    return _call_firewall(command)


@server.tool(
    "open_app",
    "Open an app by name. Works with any installed app.",
    {
        "type": "object",
        "properties": {
            "app_name": {"type": "string", "description": "App name, e.g. 'Spotify', 'Chrome', 'Settings'"},
        },
        "required": ["app_name"],
    },
)
def open_app(app_name: str) -> str:
    return _call_firewall(f"open {app_name}")


@server.tool(
    "tap_screen",
    "Tap the screen at x,y coordinates.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "X coordinate"},
            "y": {"type": "integer", "description": "Y coordinate"},
        },
        "required": ["x", "y"],
    },
)
def tap_screen(x: int, y: int) -> str:
    return _exec_action({"type": "tap", "params": {"x": x, "y": y}})


@server.tool(
    "type_text",
    "Type text into the currently focused field.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type"},
        },
        "required": ["text"],
    },
)
def type_text(text: str) -> str:
    return _exec_action({"type": "type_text", "params": {"text": text}})


@server.tool(
    "find_and_tap",
    "Find a UI element by its text label and tap it. Works for native app elements.",
    {
        "type": "object",
        "properties": {
            "text":  {"type": "string", "description": "Text or label of the element to tap"},
            "fuzzy": {"type": "boolean", "description": "Use fuzzy matching (default true)", "default": True},
        },
        "required": ["text"],
    },
)
def find_and_tap(text: str, fuzzy: bool = True) -> str:
    return _exec_action({"type": "find_and_tap", "params": {"text": text, "fuzzy": fuzzy}})


@server.tool(
    "get_screen_state",
    "Get the current screen state: current app, UI element tree (XML), and browser URL if Chrome is open. "
    "Use this to understand what's on screen before tapping or typing.",
    {
        "type": "object",
        "properties": {},
    },
)
def get_screen_state() -> str:
    return _get_screen()


@server.tool(
    "find_element_coords",
    "Given a goal (e.g. 'find the login button'), dump the screen UI and use LLM reasoning "
    "to return the x,y coordinates to tap. No vision model needed.",
    {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "What element to find, e.g. 'the search input field'"},
        },
        "required": ["goal"],
    },
)
def find_element_coords(goal: str) -> str:
    import urllib.request
    payload = json.dumps({"goal": goal}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/ui-find",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            if "x" in data:
                return f"Found at x={data['x']}, y={data['y']}"
            return "Element not found"
    except Exception as e:
        return f"Error: {e}"


@server.tool(
    "scroll",
    "Scroll the screen up or down.",
    {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
            "steps":     {"type": "integer", "description": "How many scroll steps (default 3)", "default": 3},
        },
        "required": ["direction"],
    },
)
def scroll(direction: str, steps: int = 3) -> str:
    atype = "scroll_down" if direction == "down" else "scroll_up"
    return _exec_action({"type": atype, "params": {"steps": steps}})


@server.tool(
    "key_press",
    "Press a hardware/system key.",
    {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "enum": ["home", "back", "recent", "enter", "volume_up", "volume_down", "power"],
                "description": "Key to press",
            },
        },
        "required": ["key"],
    },
)
def key_press(key: str) -> str:
    return _exec_action({"type": "key_event", "params": {"key": key}})


@server.tool(
    "run_workflow",
    "Execute a multi-step workflow. Each step is an action dict with 'type' and 'params'.",
    {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "List of action steps",
                "items": {
                    "type": "object",
                    "properties": {
                        "type":   {"type": "string"},
                        "params": {"type": "object"},
                    },
                },
            },
            "description": {"type": "string", "description": "What this workflow does"},
        },
        "required": ["steps"],
    },
)
def run_workflow(steps: list, description: str = "") -> str:
    return _exec_action({"type": "workflow", "description": description, "steps": steps})


@server.tool(
    "get_battery",
    "Get current battery level and charging status.",
    {"type": "object", "properties": {}},
)
def get_battery() -> str:
    return _exec_action({"type": "battery_status", "params": {}})


@server.tool(
    "take_screenshot",
    "Take a screenshot and save it to /sdcard/screenshot.png.",
    {"type": "object", "properties": {}},
)
def take_screenshot() -> str:
    return _exec_action({"type": "screenshot_adb", "params": {"path": "/sdcard/screenshot.png"}})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    parser.add_argument("--port", type=int, default=8001, help="HTTP port (default 8001)")
    args = parser.parse_args()

    if args.http:
        # Simple HTTP mode -- serves MCP over POST /mcp
        from http.server import HTTPServer, BaseHTTPRequestHandler

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                if self.path != "/mcp":
                    self.send_response(404); self.end_headers(); return
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                try:
                    req  = json.loads(body)
                    resp = server._handle(req)
                    out  = json.dumps(resp).encode() if resp else b"{}"
                except Exception as e:
                    out  = json.dumps({"error": str(e)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        print(f"Token Firewall MCP server -- HTTP mode on port {args.port}", file=sys.stderr)
        HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    else:
        # Default: stdio mode for OpenClaw
        server.run_stdio()
