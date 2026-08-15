---
name: token-firewall
description: >
  Control your Android device through natural language. Execute device actions,
  open apps, tap UI elements, type text, scroll, check battery, take photos, 
  and run multi-step workflows -- all at zero token cost via caching.
version: 3.0.0
metadata:
  openclaw:
    emoji: "⚡"
    requires:
      bins:
        - python
      env: []
    mcp:
      command: python
      args:
        - ~/.openclaw/workspace/skills/token-firewall/mcp_server.py
      transport: stdio
---

# Token Firewall

You have access to the Token Firewall device control system via MCP tools.
Token Firewall runs locally on the Android device and executes actions via ADB and Termux API.
Most common actions are cached -- they execute instantly at 0 tokens.

## Available Tools

- **device_command** -- Natural language device command. Use this for anything not covered by the specific tools below. Examples: "open spotify", "check battery", "set brightness to 50%", "take a photo then go home"
- **open_app** -- Open any installed app by name
- **tap_screen** -- Tap at specific x,y coordinates
- **find_and_tap** -- Find a UI element by text and tap it (no coordinates needed)
- **type_text** -- Type text into the focused field
- **scroll** -- Scroll up or down
- **key_press** -- Press home/back/recent/enter/volume keys
- **get_screen_state** -- Get current app, UI tree, and browser URL -- use before navigating
- **find_element_coords** -- Describe an element in plain English, get back x,y coordinates
- **run_workflow** -- Execute a sequence of steps
- **get_battery** -- Check battery level and status
- **take_screenshot** -- Screenshot to /sdcard/screenshot.png

## How to use

For simple commands, use **device_command**:
- "open whatsapp" → device_command("open whatsapp")
- "check my battery" → device_command("check battery")
- "go home then open spotify" → device_command("go home then open spotify")

For navigation tasks where you need to interact with the UI:
1. Call **get_screen_state** to see what's on screen
2. Call **find_and_tap** with the element text, or **find_element_coords** if text isn't clear
3. Call **type_text** if you need to enter text
4. Repeat as needed

For web forms:
1. get_screen_state to see the current UI
2. find_element_coords("the email input field") → get x,y
3. tap_screen(x, y) to focus it
4. type_text("user@example.com")
5. find_and_tap("Submit") or key_press("enter")

## Important notes

- Token Firewall must be running: `cd ~/token-firewall-v3 && bash start.sh`
- All actions run on the physical Android device
- Cached actions (battery, brightness, app opening, etc.) are instant and free
- New/unknown actions go to the LLM once, then are cached forever
- The device must have ADB wireless debugging enabled
