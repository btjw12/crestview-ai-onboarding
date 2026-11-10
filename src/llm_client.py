"""Thin LLM wrapper that returns Pydantic-validated structured output.

Both pipeline stages go through `generate_structured`, which:
  1. asks the model for a JSON object matching a Pydantic model's schema,
  2. validates the response (schema + compliance guardrails),
  3. on failure, retries once and tells the model what was wrong,
  4. raises AIResponseError if it still cannot produce valid output.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class AIResponseError(Exception):
    """The LLM failed to return a valid, guardrail-compliant response."""


# Interface for the LLM, so tests can swap in a fake one.
class JSONCompleter(Protocol):
    """Anything that can turn a system + user prompt into a JSON string."""

    model: str

    def complete_json(self, system_prompt: str, user_prompt: str) -> str: ...


# Real implementation that calls the Claude API.
class ClaudeCompleter:
    def __init__(self, api_key: str, model: str, *, timeout: float = 120.0, max_retries: int = 3):
        import anthropic  # imported lazily so tests don't need network setup

        # The Anthropic SDK retries 408 / 409 / 429 / 5xx / connection errors with backoff itself.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)
        self.model = model

    # Send one prompt to Claude and return its reply as a JSON string.
    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            # If a safety classifier declines, retry on a fallback model server-side.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise AIResponseError("LLM declined the request (refusal)")
        if response.stop_reason == "max_tokens":
            raise AIResponseError("LLM response was truncated (max_tokens)")
        # Join the text parts of the reply (ignoring any thinking blocks).
        content = "".join(b.text for b in response.content if b.type == "text")
        if not content.strip():
            raise AIResponseError("LLM returned an empty response")
        return _strip_code_fence(content)


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding ```json ... ``` fence if the model added one."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


# Tell the model exactly which JSON shape to return, using the Pydantic schema.
def schema_instructions(model_cls: type[BaseModel]) -> str:
    schema = json.dumps(model_cls.model_json_schema(), indent=2)
    return (
        "Respond with a single JSON object (no markdown, no commentary) that conforms to "
        f"this JSON Schema:\n{schema}"
    )


# Ask for JSON, validate it, and retry once with the validation error if it is invalid.
def generate_structured(
    llm: JSONCompleter,
    system_prompt: str,
    user_prompt: str,
    model_cls: type[T],
    *,
    attempts: int = 2,
) -> T:
    prompt = user_prompt
    last_error = ""
    for attempt in range(1, attempts + 1):
        raw = llm.complete_json(system_prompt, prompt)
        try:
            return model_cls.model_validate_json(raw)
        except ValidationError as exc:
            last_error = _summarise_validation_error(exc)
            logger.warning("AI output failed validation (attempt %d/%d): %s",
                           attempt, attempts, last_error)
            prompt = (
                f"{user_prompt}\n\nYour previous response was rejected because: {last_error}\n"
                "Return a corrected JSON object."
            )
    raise AIResponseError(f"{model_cls.__name__} invalid after {attempts} attempts: {last_error}")


# Turn a Pydantic error into a short "field: problem" message (first 5 errors).
def _summarise_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
