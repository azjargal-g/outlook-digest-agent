"""LLM backend: Claude via the Anthropic API, or any Anthropic-compatible gateway (e.g. one your
organization provides). Configured from .env: LLM_API_KEY, optional LLM_BASE_URL and LLM_AUTH.
Requests carry no tools: models can only return text or JSON.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class LLMError(RuntimeError):
    pass


def client() -> anthropic.Anthropic:
    key = os.getenv("LLM_API_KEY", "").strip()
    if not key:
        raise LLMError("Missing LLM_API_KEY in .env (see .env.example)")
    # Unset LLM_BASE_URL means Anthropic's API. Project-specific names avoid picking up an unrelated
    # ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL from the environment.
    common = {"base_url": os.getenv("LLM_BASE_URL", "").strip() or "https://api.anthropic.com", "max_retries": 2}
    if os.getenv("LLM_AUTH", "x-api-key").lower() == "bearer":
        return anthropic.Anthropic(auth_token=key, **common)
    return anthropic.Anthropic(api_key=key, **common)


@contextmanager
def _api_errors(model: str):
    try:
        yield
    except anthropic.AuthenticationError as e:
        raise LLMError(f"API key rejected (401); for a gateway, try LLM_AUTH=bearer. {e.message}") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"404: check LLM_BASE_URL (with/without /v1) and the model ID '{model}'. {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"LLM API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Could not reach the LLM API: {e}") from e


def _check(response, max_tokens: int) -> None:
    if response.stop_reason == "refusal":
        raise LLMError(f"Model declined: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise LLMError(f"Hit max_tokens={max_tokens} before finishing")


def complete(system: str, user: str, model: str, max_tokens: int = 4096) -> str:
    """One request, plain text out."""
    with _api_errors(model):
        response = client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    _check(response, max_tokens)
    return "".join(b.text for b in response.content if b.type == "text").strip()


def parse[T: BaseModel](system: str, user: str, model: str, output_format: type[T],
                        max_tokens: int = 16000, effort: str | None = None) -> T:
    """One request, validated JSON out (structured outputs)."""
    extra = {"output_config": {"effort": effort}} if effort else {}
    with _api_errors(model):
        response = client().messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
            **extra,
        )
    _check(response, max_tokens)
    return response.parsed_output
