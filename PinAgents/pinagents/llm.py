"""One small client for every provider, plus an offline mock so the whole pipeline runs without a key."""
from __future__ import annotations

import json
import re

from .default_config import DEFAULT_MODELS


class LLM:
    def __init__(self, config: dict):
        self.provider = config["llm_provider"]
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(f"Unknown llm_provider '{self.provider}'. Use one of {list(DEFAULT_MODELS)}")
        defaults = DEFAULT_MODELS[self.provider]
        self.models = {
            "quick": config.get("quick_think_llm") or defaults["quick"],
            "deep": config.get("deep_think_llm") or defaults["deep"],
        }
        self._client = None
        self._ollama_url = config.get("ollama_base_url")

    # ---- public ---------------------------------------------------------
    def text(self, system: str, user: str, tier: str = "quick", task: str = "", payload=None) -> str:
        if self.provider == "mock":
            from . import mock_llm
            return json.dumps(mock_llm.respond(task, payload))
        return self._call(system, user, self.models[tier])

    def json(self, system: str, user: str, tier: str = "quick", task: str = "", payload=None):
        """Ask for JSON and parse it. Retries once if the reply isn't valid JSON."""
        raw = self.text(system, user, tier, task, payload)
        try:
            return _extract_json(raw)
        except ValueError:
            raw = self.text(
                system,
                user + "\n\nYour last reply was not valid JSON. Reply with ONLY the JSON, nothing else.",
                tier, task, payload,
            )
            return _extract_json(raw)

    # ---- providers ------------------------------------------------------
    def _call(self, system: str, user: str, model: str) -> str:
        if self.provider == "anthropic":
            if self._client is None:
                from anthropic import Anthropic
                self._client = Anthropic()
            resp = self._client.messages.create(
                model=model, max_tokens=4000, system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

        # openai and ollama share the OpenAI-compatible chat API
        if self._client is None:
            from openai import OpenAI
            if self.provider == "ollama":
                self._client = OpenAI(base_url=self._ollama_url, api_key="ollama")
            else:
                self._client = OpenAI()
        resp = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


def _extract_json(raw: str):
    raw = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # fall back to the first {...} or [...] block
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = raw.find(open_c), raw.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("No valid JSON in model reply")
