# CONTRACT.md: Token Firewall v3 interface spec

This document describes the external HTTP contracts for **Token Firewall v3**.

Token Firewall is an OpenAI-compatible gateway that can also execute device
actions via platform "hands" (Android, desktop, etc.). It sits between a
client (any OpenAI-compatible app, CLI, custom apps) and one or more upstream LLM providers.

---

## 1) OpenAI-compatible HTTP API

Base URL (default):

```text
http://127.0.0.1:8000
```

### 1.1 `/v1/chat/completions` (POST)

OpenAI chat completions interface. Token Firewall accepts the usual OpenAI
fields and adds some optional, firewall-specific metadata in the response.

**Request (minimal example):**

```http
POST /v1/chat/completions HTTP/1.1
Host: 127.0.0.1:8000
Content-Type: application/json
Authorization: Bearer none

{
  "model": "firewall",
  "messages": [
    { "role": "user", "content": "open spotify" }
  ]
}
```

**Response (shape):**

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "firewall",
  "choices": [
    {
      "index": 0,
      "finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "…"
      }
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  },
  "firewall": {
    "source": "cache" | "llm" | "pack" | "actions-only",
    "platform": "android" | "desktop" | "ios" | "unknown",
    "cache_hit": true,
    "tokens_upstream": 0,
    "actions_executed": [
      {
        "kind": "tap" | "type" | "shell" | "open_url" | "launch_app" | "custom",
        "ok": true,
        "summary": "launched com.spotify.android.music"
      }
    ]
  }
}
```

Notes:

- The top-level shape matches OpenAI chat completions so existing clients can
  usually drop Token Firewall in without changes.
- The extra `firewall` object is optional and may be omitted in minimal modes.
- `tokens_upstream` reflects what the **upstream LLM** used on a miss; it is
  `0` for pure cache hits.

#### Streaming

If `stream: true` is provided, Token Firewall uses OpenAI SSE streaming:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion.chunk",
  "choices": [
    { "index": 0, "delta": { "content": "…" }, "finish_reason": null }
  ]
}
```

The `firewall` metadata may be sent either alongside the final chunk or as a
separate event (implementation detail; check the README for current behavior).

---

### 1.2 `/v1/models` (GET)

Lists the logical models exposed by Token Firewall.

**Request:**

```http
GET /v1/models HTTP/1.1
```

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "firewall",
      "object": "model",
      "owned_by": "token-firewall",
      "metadata": {
        "description": "Zero-token gateway over one or more upstream LLMs"
      }
    }
  ]
}
```

The single logical model `firewall` can fan out to multiple upstream providers
based on config and fallback rules.

---

### 1.3 `/v1/capabilities` (GET)

Describes what the current platform can actually do.

**Response:**

```json
{
  "platform": "android",
  "hands": {
    "can_shell": true,
    "can_launch_apps": true,
    "can_open_urls": true,
    "can_tap": true,
    "can_type": true,
    "can_swipe": true
  },
  "notes": "Android Termux + ADB detected"
}
```

This is primarily diagnostic and for tooling.

---

### 1.4 `/v1/ui-find` (POST)

Helper endpoint for UI automation. Uses the current screen dump and an LLM
(or cached knowledge) to choose tap coordinates.

**Request:**

```json
{
  "goal": "tap the login button",
  "context": "optional extra description of the app/screen"
}
```

**Response:**

```json
{
  "x": 540,
  "y": 1200,
  "confidence": 0.83,
  "notes": "primary filled button with text 'Log in'"
}
```

Clients can then execute the tap themselves (e.g. via ADB) or hand it back to
Token Firewall as an action.

---

### 1.5 `/v1/export-pack` (POST)

Exports a subset of the learned cache into a static `pack` that can be checked
in to `packs/` and reused across installs.

**Request:**

```json
{
  "name": "android-core",
  "filter": {
    "platform": "android",
    "min_hits": 3
  }
}
```

**Response:**

```json
{
  "ok": true,
  "path": "packs/android/android-core.json",
  "entries": 42
}
```

---

### 1.6 `/health` (GET)

Simple status endpoint.

```json
{
  "ok": true,
  "uptime_s": 12345,
  "platform": "android",
  "stats": {
    "cache_hits": 120,
    "cache_misses": 17,
    "tokens_saved_estimate": 94200
  }
}
```

---

## 2) Device actions (high level)

Token Firewall does not currently expose a public "raw actions" HTTP endpoint;
Actions are usually decided and executed inside the firewall as part of a
chat/completions call.

Internally, actions sent to a platform hands implementation have a generic
shape like:

```json
{
  "kind": "tap",
  "x": 540,
  "y": 1200,
  "meta": { "reason": "tap login button" }
}
```

Other examples:

```json
{ "kind": "type", "text": "hello world" }
{ "kind": "launch_app", "package": "com.spotify.android.music" }
{ "kind": "open_url", "url": "https://example.com" }
{ "kind": "shell", "command": "ls -la" }
```

The exact shape is internal and may evolve, but the contract is:

- A hands implementation **must** return a simple result object with:
  - `ok: true | false`
  - optional `error` string
  - optional `summary` string
- Hands are allowed to be conservative and reject actions they consider unsafe
  based on their platform rules.

---

## 3) Configuration surface (env)

Key environment variables (see README for full list):

- `TF_PLATFORM` -- auto-detected platform override (`android`, `linux`, etc.)
- `TF_LLM_BASE_URL` -- upstream LLM base URL
- `TF_LLM_API_KEY` -- upstream LLM API key
- `TF_LLM_MODEL` -- primary upstream model
- `TF_LLM_FALLBACKS` -- semicolon-separated fallback chain
- `TF_HOST`, `TF_PORT` -- bind host/port
- `TF_FUZZY_THRESHOLD` -- cache similarity threshold
- `TF_STALE_DAYS` -- days before learned entries expire
- `TF_DISABLE_ADB`, `TF_DISABLE_TERMUX`, `TF_DISABLE_ACTIONS` -- safety toggles

These are considered part of the public configuration contract for v3.

---

## 4) Versioning

This contract describes **Token Firewall v3**. Breaking changes to the HTTP
surface (beyond adding new fields) should bump the major version in the docs
and changelog.
