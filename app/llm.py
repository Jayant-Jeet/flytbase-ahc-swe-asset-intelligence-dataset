"""LLM provider interface: plain completion, plus streaming chat with function calling.

Select a backend with LLM_PROVIDER = gemini | openrouter | ollama (model via LLM_MODEL).

Normalised chat messages (provider-independent):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}], "raw": <provider extras>}
  {"role": "tool", "tool_call_id": str, "name": str, "content": <json string>}

`stream_chat` yields events: {"type": "text", "text": str} and {"type": "tool_call", "id", "name", "args", "raw"}.
"""
from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from typing import Iterator

import httpx

_DROP_KEYS = {"additionalProperties", "default", "$schema"}


def clean_schema(s):
    """Strip JSON-schema keys that Gemini's OpenAPI subset rejects."""
    if isinstance(s, dict):
        return {k: clean_schema(v) for k, v in s.items() if k not in _DROP_KEYS}
    if isinstance(s, list):
        return [clean_schema(v) for v in s]
    return s


def _loads(s):
    try:
        v = json.loads(s) if isinstance(s, str) else s
    except json.JSONDecodeError:
        return {"text": s}
    return v if isinstance(v, dict) else {"result": v}


class LLMProvider(ABC):
    name = "base"
    model = ""

    @abstractmethod
    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str: ...

    @abstractmethod
    def stream_chat(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[dict]: ...


def _sse_lines(resp: httpx.Response) -> Iterator[dict]:
    for line in resp.iter_lines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                yield json.loads(payload)


def _raise(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        resp.read()
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self) -> None:
        self.key = os.environ["GOOGLE_API_KEY"]
        self.model = os.getenv("LLM_MODEL", "gemma-4-31b-it")
        self.base = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        r = httpx.post(f"{self.base}/models/{self.model}:generateContent", json=body,
                       headers={"x-goog-api-key": self.key}, timeout=180)
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    @staticmethod
    def _contents(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                parts = []
                if m.get("content"):
                    parts.append({"text": m["content"]})
                parts += m.get("raw") or [{"functionCall": {"name": c["name"], "args": c["args"]}}
                                          for c in m.get("tool_calls", [])]
                out.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                part = {"functionResponse": {"name": m["name"], "response": _loads(m["content"])}}
                if out and out[-1]["role"] == "user" and "functionResponse" in out[-1]["parts"][0]:
                    out[-1]["parts"].append(part)
                else:
                    out.append({"role": "user", "parts": [part]})
        return out

    def stream_chat(self, system, messages, tools):
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": self._contents(messages),
                "generationConfig": {"temperature": 0}}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t["description"], "parameters": clean_schema(t["parameters"])}
                for t in tools]}]
        url = f"{self.base}/models/{self.model}:streamGenerateContent?alt=sse"
        with httpx.stream("POST", url, json=body, headers={"x-goog-api-key": self.key}, timeout=300) as r:
            _raise(r)
            for chunk in _sse_lines(r):
                cands = chunk.get("candidates") or []
                for p in (cands[0].get("content", {}).get("parts", []) if cands else []):
                    if p.get("thought"):
                        continue
                    if "functionCall" in p:
                        fc = p["functionCall"]
                        yield {"type": "tool_call", "id": f"call_{uuid.uuid4().hex[:8]}", "name": fc["name"],
                               "args": fc.get("args") or {}, "raw": p}
                    elif p.get("text"):
                        yield {"type": "text", "text": p["text"]}


class OpenAICompatProvider(LLMProvider):
    """OpenRouter and Ollama (both expose the OpenAI chat-completions API)."""

    def __init__(self, name: str, base_url: str, key: str, model: str) -> None:
        self.name, self.base_url, self.key, self.model = name, base_url.rstrip("/"), key, model

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        body: dict = {"model": self.model, "temperature": 0,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers, timeout=300)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": "user", "content": m["content"]})
        return out

    def stream_chat(self, system, messages, tools):
        body = {"model": self.model, "temperature": 0, "stream": True, "messages": self._messages(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        pending: dict[int, dict] = {}
        with httpx.stream("POST", f"{self.base_url}/chat/completions", json=body, headers=self._headers,
                          timeout=300) as r:
            _raise(r)
            for chunk in _sse_lines(r):
                ch = (chunk.get("choices") or [{}])[0]
                d = ch.get("delta") or {}
                if d.get("content"):
                    yield {"type": "text", "text": d["content"]}
                for tc in d.get("tool_calls") or []:
                    p = pending.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                    p["id"] = tc.get("id") or p["id"]
                    fn = tc.get("function") or {}
                    p["name"] += fn.get("name") or ""
                    p["args"] += fn.get("arguments") or ""
        for p in pending.values():
            args = json.loads(p["args"]) if p["args"].strip() else {}
            yield {"type": "tool_call", "id": p["id"] or f"call_{uuid.uuid4().hex[:8]}", "name": p["name"],
                   "args": args, "raw": None}


def get_provider() -> LLMProvider:
    kind = os.getenv("LLM_PROVIDER", "gemini").lower()
    if kind == "gemini":
        return GeminiProvider()
    if kind == "openrouter":
        return OpenAICompatProvider("openrouter", "https://openrouter.ai/api/v1",
                                    os.environ["OPENROUTER_API_KEY"],
                                    os.getenv("LLM_MODEL", "google/gemma-4-31b-it:free"))
    if kind == "ollama":
        return OpenAICompatProvider("ollama", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                                    "ollama", os.getenv("LLM_MODEL", "gemma4:31b"))
    raise ValueError(f"Unknown LLM_PROVIDER: {kind}")


def llm_configured() -> bool:
    kind = os.getenv("LLM_PROVIDER", "gemini").lower()
    if kind == "gemini":
        return bool(os.getenv("GOOGLE_API_KEY"))
    if kind == "openrouter":
        return bool(os.getenv("OPENROUTER_API_KEY"))
    return kind == "ollama"
