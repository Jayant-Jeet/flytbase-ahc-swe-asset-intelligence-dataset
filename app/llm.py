"""Minimal LLM provider interface (Phase 1 needs plain completion only; tool calling arrives in Phase 2).

Select a backend with LLM_PROVIDER = gemini | openrouter | ollama.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

import httpx


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str: ...


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self) -> None:
        self.key = os.environ["GOOGLE_API_KEY"]
        self.model = os.getenv("LLM_MODEL", "gemma-4-31b-it")

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        r = httpx.post(url, json=body, headers={"x-goog-api-key": self.key}, timeout=180)
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)


class OpenAICompatProvider(LLMProvider):
    """OpenRouter and Ollama (both expose the OpenAI chat-completions API)."""

    def __init__(self, name: str, base_url: str, key: str, model: str) -> None:
        self.name, self.base_url, self.key, self.model = name, base_url.rstrip("/"), key, model

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        body: dict = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(f"{self.base_url}/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {self.key}"}, timeout=300)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


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
