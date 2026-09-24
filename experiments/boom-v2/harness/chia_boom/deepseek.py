from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, List, Optional

import requests

from chia.base.ChiaFunction import ChiaFunction
from chia.base.llm_call import LLMCallBase, QueryResult
from chia.base.tools.ChiaTool import ChiaTool


class DeepSeekOfficialLLM(LLMCallBase):
    """Official DeepSeek JSON backend dispatched by CHIA.

    The key is read only from the worker environment. ``stream_result`` carries
    the complete provider response so the campaign can persist provider usage
    and audit the exact successful HTTP response.
    """

    def __init__(
        self,
        *,
        system_message: str,
        model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com/v1",
        temperature: float = 0.2,
        max_tokens: int = 32_000,
        timeout_seconds: int = 900,
        attempts: int = 3,
        api_key_env: str = "DEEPSEEK_API_KEY",
    ):
        super().__init__(system_message=system_message)
        if base_url.rstrip("/") != "https://api.deepseek.com/v1":
            raise ValueError("DeepSeekOfficialLLM only accepts the official endpoint")
        if model != "deepseek-v4-pro":
            raise ValueError("unexpected model")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.api_key_env = api_key_env

    @ChiaFunction(resources={"deepseek_creds": 0.01}, max_retries=0)
    def prompt(
        self,
        user_message: str,
        tools: Optional[List[ChiaTool]] = [],
    ) -> QueryResult:
        if tools:
            raise ValueError("the BOOM proposal protocol does not expose tools")
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing worker environment variable {self.api_key_env}")
        request_id = str(uuid.uuid4())
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        attempts: list[dict[str, Any]] = []
        started = time.monotonic()
        response_payload: dict[str, Any] | None = None
        last_error = ""
        session = requests.Session()
        session.trust_env = False
        for attempt in range(1, self.attempts + 1):
            t0 = time.monotonic()
            try:
                response = session.post(
                    self.base_url + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                        "X-Client-Request-Id": request_id,
                    },
                    json=body,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": response.status_code,
                        "elapsed_seconds": time.monotonic() - t0,
                        "retry_after": response.headers.get("retry-after"),
                    }
                )
                if response.status_code >= 400:
                    last_error = response.text[:16_000]
                    if response.status_code not in (408, 409, 429) and response.status_code < 500:
                        break
                    if attempt < self.attempts:
                        delay = 15 * 2 ** (attempt - 1)
                        retry_after = response.headers.get("retry-after")
                        if retry_after and retry_after.isdigit():
                            delay = max(delay, int(retry_after))
                        time.sleep(min(delay, 60))
                        continue
                    break
                response_payload = response.json()
                content = response_payload["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("provider returned empty content")
                metadata = {
                    "request_id": request_id,
                    "attempts": attempts,
                    "elapsed_seconds": time.monotonic() - started,
                    "provider_model": response_payload.get("model"),
                    "usage": response_payload.get("usage"),
                    "request": body,
                    "response": response_payload,
                }
                return QueryResult(
                    result=content,
                    returncode=0,
                    stderr=json.dumps({"attempts": attempts}),
                    stream_result=json.dumps(metadata),
                    success=True,
                )
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": None,
                        "elapsed_seconds": time.monotonic() - t0,
                        "error_type": type(exc).__name__,
                    }
                )
                # A 200 response has consumed tokens; do not generate another
                # full-price response for a schema/content failure.
                if response_payload is not None or attempt == self.attempts:
                    break
                time.sleep(min(15 * 2 ** (attempt - 1), 60))
        metadata = {
            "request_id": request_id,
            "attempts": attempts,
            "elapsed_seconds": time.monotonic() - started,
            "request": body,
            "response": response_payload,
        }
        return QueryResult(
            result="",
            returncode=1,
            stderr=last_error,
            stream_result=json.dumps(metadata),
            success=False,
        )


class DeepSeekOfficialToolLLM(LLMCallBase):
    """One auditable turn of a direct DeepSeek tool-using conversation.

    CHIA schedules each provider call, while the campaign controller keeps the
    conversation and executes allow-listed hardware tools.  This mirrors the
    upstream timing flow's persistent coding-agent session without depending
    on OpenCode or exposing a shell to the provider.
    """

    def __init__(
        self,
        *,
        system_message: str,
        model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com/v1",
        max_tokens: int = 32_000,
        timeout_seconds: int = 900,
        attempts: int = 3,
        api_key_env: str = "DEEPSEEK_API_KEY",
        reasoning_effort: str = "high",
    ):
        super().__init__(system_message=system_message)
        if base_url.rstrip("/") != "https://api.deepseek.com/v1":
            raise ValueError("DeepSeekOfficialToolLLM only accepts the official endpoint")
        if model != "deepseek-v4-pro":
            raise ValueError("unexpected model")
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("unexpected reasoning effort")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.api_key_env = api_key_env
        self.reasoning_effort = reasoning_effort

    def prompt(
        self,
        user_message: str,
        tools: Optional[List[ChiaTool]] = [],
    ) -> QueryResult:
        """Satisfy LLMCallBase; interactive runs must call ``chat_turn``."""
        raise NotImplementedError("use chat_turn for the tool-using DeepSeek agent")

    @ChiaFunction(resources={"deepseek_creds": 0.01}, max_retries=0)
    def chat_turn(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]],
    ) -> QueryResult:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing worker environment variable {self.api_key_env}")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_message}, *messages],
            "tools": tool_specs,
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            "thinking": {"type": "enabled"},
            "reasoning_effort": self.reasoning_effort,
        }
        request_id = str(uuid.uuid4())
        attempts: list[dict[str, Any]] = []
        started = time.monotonic()
        response_payload: dict[str, Any] | None = None
        last_error = ""
        session = requests.Session()
        session.trust_env = False
        for attempt in range(1, self.attempts + 1):
            t0 = time.monotonic()
            try:
                response = session.post(
                    self.base_url + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                        "X-Client-Request-Id": request_id,
                    },
                    json=body,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
                attempts.append({
                    "attempt": attempt,
                    "status": response.status_code,
                    "elapsed_seconds": time.monotonic() - t0,
                    "retry_after": response.headers.get("retry-after"),
                })
                if response.status_code >= 400:
                    last_error = response.text[:16_000]
                    if response.status_code not in (408, 409, 429) and response.status_code < 500:
                        break
                    if attempt < self.attempts:
                        delay = 15 * 2 ** (attempt - 1)
                        retry_after = response.headers.get("retry-after")
                        if retry_after and retry_after.isdigit():
                            delay = max(delay, int(retry_after))
                        time.sleep(min(delay, 60))
                        continue
                    break
                response_payload = response.json()
                message = response_payload["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise ValueError("provider returned an invalid assistant message")
                metadata = {
                    "request_id": request_id,
                    "attempts": attempts,
                    "elapsed_seconds": time.monotonic() - started,
                    "provider_model": response_payload.get("model"),
                    "usage": response_payload.get("usage"),
                    "request": body,
                    "response": response_payload,
                }
                return QueryResult(
                    result=json.dumps(message), returncode=0,
                    stderr=json.dumps({"attempts": attempts}),
                    stream_result=json.dumps(metadata), success=True,
                )
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempts.append({
                    "attempt": attempt, "status": None,
                    "elapsed_seconds": time.monotonic() - t0,
                    "error_type": type(exc).__name__,
                })
                if response_payload is not None or attempt == self.attempts:
                    break
                time.sleep(min(15 * 2 ** (attempt - 1), 60))
        metadata = {
            "request_id": request_id, "attempts": attempts,
            "elapsed_seconds": time.monotonic() - started,
            "request": body, "response": response_payload,
        }
        return QueryResult(
            result="", returncode=1, stderr=last_error,
            stream_result=json.dumps(metadata), success=False,
        )


def parse_result(result: QueryResult) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse a CHIA QueryResult into validated proposal and provider metadata."""
    metadata = json.loads(result.stream_result)
    if not result.success:
        raise RuntimeError(result.stderr or "DeepSeek request failed")
    text = result.result.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    return json.loads(text), metadata
