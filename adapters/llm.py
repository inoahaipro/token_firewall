"""
adapters/llm.py -- LLM adapter with automatic fallback chain.

Primary provider: TF_LLM_BASE_URL / TF_LLM_API_KEY / TF_LLM_MODEL
Fallbacks:        TF_LLM_FALLBACKS = "url|key|model;url|key|model"

Returns (text, tokens) always. Raises RuntimeError only if ALL providers fail.
"""
import json
import urllib.request
import urllib.error
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import core.config as cfg


class _Provider:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 45):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.model    = model
        self.timeout  = timeout
        self.name     = base_url.split("//")[-1].split("/")[0]

    def _headers(self) -> dict:
        return {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "Bearer none",
            "User-Agent":    "TokenFirewall/3.0",
        }

    def complete(self, prompt: str, system: str, history: list = None) -> tuple[str, int]:
        messages = [{"role": "system", "content": system}]
        # Add conversation history for memory (skip last user msg, we add it below)
        if history:
            for msg in history[:-1]:  # exclude last since it's the current prompt
                if msg.get("role") in ("user", "assistant"):
                    messages.append(msg)
        messages.append({"role": "user", "content": prompt})
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload, headers=self._headers(), method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data   = json.loads(resp.read())
            tokens = int((data.get("usage") or {}).get("total_tokens", 0) or 0)
            text   = data["choices"][0]["message"]["content"]
            return text, tokens


class LLMAdapter:

    def __init__(self):
        self._providers = self._build()
        print(f"[LLM] {len(self._providers)} provider(s) -- primary: {self._providers[0].name if self._providers else 'none'}")

    def _build(self) -> list[_Provider]:
        providers = []
        if cfg.LLM_BASE_URL and cfg.LLM_MODEL:
            providers.append(_Provider(cfg.LLM_BASE_URL, cfg.LLM_API_KEY, cfg.LLM_MODEL, cfg.LLM_TIMEOUT))
        for fb in cfg.LLM_FALLBACKS.split(";"):
            parts = [p.strip() for p in fb.strip().split("|")]
            if len(parts) == 3 and parts[0] and parts[2]:
                providers.append(_Provider(*parts, timeout=cfg.LLM_TIMEOUT))
        return providers

    def complete(self, prompt: str, system: str = "", history: list = None) -> tuple[str, int]:
        system = system or cfg.SYSTEM_PROMPT
        last_err = None
        for i, p in enumerate(self._providers):
            try:
                print(f"[LLM] → {p.name} ({p.model})")
                text, tokens = p.complete(prompt, system, history=history)
                if i > 0:
                    print(f"[LLM] fallback #{i} succeeded")
                return text, tokens
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                last_err = f"HTTP {e.code}: {body[:150]}"
                print(f"[LLM] {p.name} failed: {last_err}")
                if e.code not in (429, 500, 502, 503, 504):
                    break   # don't retry on bad requests
            except Exception as e:
                last_err = str(e)
                print(f"[LLM] {p.name} error: {last_err}")
        raise RuntimeError(f"All LLM providers failed. Last: {last_err}")
