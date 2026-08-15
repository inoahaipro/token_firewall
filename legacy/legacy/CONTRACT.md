# CONTRACT.md: wrangle-imprint interface spec (v2.0.0)

This document describes the JSON/CLI contracts used between:

1. OpenClaw/dispatcher ↔ `imprint.py`
2. `imprint.py` ↔ `wrangle.py`

---

## 1) OpenClaw / caller ↔ IMPRINT

### Invocation

```bash
python imprint.py ask "<intent>" [--dry] [--confirmed] [--queue]
```

Flags:

- `--dry`: return planned/resolved steps without executing.
- `--confirmed`: allow destructive actions.
- `--queue`: enqueue task in SQLite queue instead of executing now.

### Common response shape

IMPRINT prints one JSON object to stdout.

#### Cache hit

```json
{
  "source": "cache",
  "plan_id": "a1b2c3d4e5f6a7b8",
  "similarity": 0.91,
  "tokens": 0,
  "duration_ms": 3200,
  "steps_taken": 5
}
```

#### New LLM plan

```json
{
  "source": "llm",
  "plan_id": "b2c3d4e5f6a7b8c9",
  "tokens": 812,
  "duration_ms": 7400,
  "steps_taken": 5,
  "trusted": false
}
```

#### Mid-task replan

```json
{
  "source": "replan",
  "plan_id": "a1b2c3d4e5f6a7b8",
  "tokens": 540,
  "duration_ms": 9100,
  "steps_taken": 8
}
```

#### Failure

```json
{
  "source": "llm",
  "success": false,
  "error": "llm_key_missing"
}
```

> `llm_key_missing` is the legacy code string used when IMPRINT has no usable OpenClaw session configured.

#### Dry run

```json
{
  "source": "cache",
  "plan_id": "a1b2c3d4e5f6a7b8",
  "similarity": 0.88,
  "dry_run": true
}
```

#### Queued

```json
{
  "queued": true,
  "intent": "turn off wifi"
}
```

### `plans-json`

```bash
python imprint.py plans-json
```

Returns an array with plan metadata (`id`, `template`, `trusted`, `confirm_count`, `hits`, `failures`, etc.).

### Error codes

- `adb_unavailable`
- `state_parse_error`
- `target_not_found`
- `launch_failed`
- `input_failed`
- `screen_drift`
- `unsafe_action_blocked`
- `max_steps_exceeded`
- `task_timeout`
- `llm_key_missing` (legacy name; currently indicates no available OpenClaw session)

---

## 2) IMPRINT ↔ wrangle

IMPRINT shells out to wrangle:

```bash
python wrangle.py get_state --task "<intent>"
python wrangle.py do_action --json '<action_json>'
```

### `get_state` response

```json
{
  "screen_hash": "a3f9b2e14c7d",
  "state_signature": "8fa1c3b29e01",
  "foreground_app": "com.google.android.youtube",
  "screen_summary": "14 ranked elements from 87 raw UI nodes",
  "raw_count": 87,
  "elements": [
    {
      "id": 12,
      "label": "Search YouTube",
      "text": "Search YouTube",
      "content-desc": "Search YouTube",
      "resource-id": "search_edit_text",
      "class": "EditText",
      "role": "input",
      "x": 540,
      "y": 112,
      "bounds": "[0,88][1080,136]",
      "score": 14.5
    }
  ]
}
```

### `do_action` request shapes

```json
{ "action": "tap", "x": 540, "y": 1200 }
{ "action": "type", "text": "hello" }
{ "action": "swipe", "x1": 100, "y1": 800, "x2": 100, "y2": 200, "ms": 300 }
{ "action": "scroll", "direction": "down", "amount": 800 }
{ "action": "keyevent", "key": "KEYCODE_ENTER" }
{ "action": "back" }
{ "action": "launch", "app": "youtube" }
{ "action": "open_url", "url": "https://m.youtube.com" }
{ "action": "done", "reason": "task complete" }
```

### `do_action` response

Success:

```json
{ "ok": true, "executed": "tap", "x": 540, "y": 1200 }
```

Failure:

```json
{ "ok": false, "error": "unsupported action: hover" }
```

### Element targeting (inside IMPRINT execution)

IMPRINT can resolve `target` fields (e.g., `{"text":"Search"}`, `{"id":"search_edit_text"}`, `{"desc":"Search"}`) against `get_state.elements`, then emits concrete tap coordinates to `do_action`.

---

## 3) Queue and logs

`imprint.py` persists:

- `queue` (offline tasks: `pending`, `done`, `failed`, `error`)
- `task_log` (per-task source/result/timing)
- `step_log` (per-step action/success/screen_changed/error)

Flush queue:

```bash
python imprint.py flush
```

---

## Versioning

| Component | Version |
|---|---|
| `imprint.py` | 2.0.0 |
| `wrangle.py` | 2.0.0 |
| contract | 2.0.0 |
