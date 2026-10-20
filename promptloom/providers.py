"""Hosted model providers, reached with the user's own API key.

Anthropic goes through its SDK. Everything else speaks the OpenAI-compatible
chat completions protocol, so a provider is just a base URL.
"""

from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .enrich import EnrichError, _json_from

# name -> (label, base URL of the OpenAI-compatible API)
OPENAI_COMPATIBLE = {
    "openai": ("OpenAI", "https://api.openai.com/v1"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1"),
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "groq": ("Groq", "https://api.groq.com/openai/v1"),
    "mistral": ("Mistral", "https://api.mistral.ai/v1"),
    "xai": ("xAI", "https://api.x.ai/v1"),
    "together": ("Together", "https://api.together.xyz/v1"),
}
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
# Models whose safety classifiers can decline a request; let the API reroute.
ANTHROPIC_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def names() -> list[str]:
    return ["anthropic", *OPENAI_COMPATIBLE, "custom"]


def label(provider: str) -> str:
    if provider == "anthropic":
        return "Anthropic"
    return OPENAI_COMPATIBLE.get(provider, (provider,))[0]


# stored settings

def load_file(path: Path) -> dict:
    import tomllib

    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def save_file(path: Path, data: dict) -> None:
    """Write provider settings, readable only by the user: the file holds API keys."""
    lines = ["# Written by `promptloom provider`. Holds API keys: keep it private."]
    for key in ("provider", "model", "base_url"):
        if data.get(key):
            lines.append(f"{key} = {json.dumps(data[key])}")
    lines.append("\n[keys]")
    for name, value in sorted(data.get("keys", {}).items()):
        lines.append(f"{name} = {json.dumps(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "set"


# backends

def _strict(schema: dict) -> dict:
    """Structured outputs need every object closed to extra properties."""
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


class Anthropic:
    name = "anthropic"

    def __init__(self, key: str, model: str, timeout: float = 180):
        try:
            import anthropic
        except ImportError as exc:
            raise EnrichError(
                "the anthropic package is not installed "
                "(uv tool install --force --with anthropic --editable <promptloom dir>)") from exc
        self._sdk = anthropic
        self.client = anthropic.Anthropic(api_key=key, timeout=timeout)
        self.model = model or ANTHROPIC_DEFAULT_MODEL

    def ready(self) -> str:
        return ""

    def complete(self, prompt: str, schema: dict) -> dict:
        sdk = self._sdk
        request = dict(
            model=self.model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _strict(schema)}},
        )
        try:
            if self.model in ANTHROPIC_FALLBACK_MODELS:
                response = self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **request)
            else:
                response = self.client.messages.create(**request)
        except sdk.AuthenticationError as exc:
            raise EnrichError("Anthropic rejected the API key") from exc
        except sdk.PermissionDeniedError as exc:
            raise EnrichError(f"Anthropic: key lacks permission: {exc.message}") from exc
        except sdk.NotFoundError as exc:
            raise EnrichError(f"Anthropic: unknown model '{self.model}'") from exc
        except sdk.RateLimitError as exc:
            raise EnrichError("Anthropic: rate limited") from exc
        except sdk.APIStatusError as exc:
            raise EnrichError(f"Anthropic {exc.status_code}: {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise EnrichError(f"Anthropic unreachable: {exc}") from exc
        if response.stop_reason == "refusal":
            raise EnrichError("Anthropic declined to summarise this session")
        if response.stop_reason == "max_tokens":
            raise EnrichError("Anthropic: reply was cut off")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return _json_from(text)

    def models(self) -> list[str]:
        try:
            return [m.id for m in self.client.models.list()]
        except self._sdk.APIError as exc:
            raise EnrichError(f"Anthropic: {exc}") from exc


class OpenAICompatible:
    name = "openai-compatible"

    def __init__(self, key: str, model: str, base_url: str, timeout: float = 180):
        self.key = key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._json_mode = True  # until the provider rejects response_format

    def ready(self) -> str:
        return ""

    def _call(self, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code in (401, 403):
                raise EnrichError(f"the provider rejected the API key ({exc.code})") from exc
            raise EnrichError(f"provider returned {exc.code}: {detail}") from exc
        except (OSError, ValueError) as exc:
            raise EnrichError(f"provider unreachable: {exc}") from exc

    def complete(self, prompt: str, schema: dict) -> dict:
        prompt += ("\n\nReply with only a JSON object matching this JSON schema:\n"
                   + json.dumps(schema))
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            reply = self._call("/chat/completions", body)
        except EnrichError as exc:
            if self._json_mode and "400" in str(exc) and "response_format" in str(exc):
                self._json_mode = False
                return self.complete(prompt.rsplit("\n\nReply with only", 1)[0], schema)
            raise
        try:
            text = reply["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise EnrichError("provider returned an unexpected response") from exc
        return _json_from(text)

    def models(self) -> list[str]:
        data = self._call("/models").get("data", [])
        return sorted(m["id"] for m in data if isinstance(m, dict) and m.get("id"))


def build(cfg: dict):
    """The backend for `[enrich] backend = "api"`."""
    enrich = cfg["enrich"]
    provider = enrich.get("provider", "")
    key = cfg.get("keys", {}).get(provider) or os.environ.get("PROMPTLOOM_API_KEY", "")
    if not key:
        raise EnrichError(f"no API key stored for '{provider}' (promptloom provider set {provider})")
    timeout = enrich.get("timeout_seconds", 180)
    if provider == "anthropic":
        return Anthropic(key, enrich.get("model", ""), timeout)
    base_url = enrich.get("base_url") or OPENAI_COMPATIBLE.get(provider, ("", ""))[1]
    if not base_url:
        raise EnrichError(f"provider '{provider}' needs a base_url")
    if not enrich.get("model"):
        raise EnrichError(f"provider '{provider}' needs a model")
    return OpenAICompatible(key, enrich["model"], base_url, timeout)
