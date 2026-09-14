"""OpenAI-compatible Chat Completions adapter with strict response validation."""

import re
from typing import Any

import httpx

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.llm import CompletionRequest, CompletionResult
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

_THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


class OpenAICompatibleLanguageModel:
    """Call one OpenAI-compatible ``/chat/completions`` endpoint.

    Timeout and retry policy deliberately live in ``BoundedLanguageModel`` so all
    language-model adapters share one budget boundary.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip() or not api_key.strip() or not model.strip():
            raise ValueError("OpenAI-compatible LLM configuration must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("LLM timeout must be positive")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._health = ProviderHealth.UNKNOWN
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.LLM,
            name="openai_compatible",
            version=self._model,
            capabilities=frozenset(
                {"chat-completions", "usage", "system-prompt", "json-mode"}
            ),
            is_remote=True,
            health=ProviderHealth.UNAVAILABLE if self._closed else self._health,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        if self._closed:
            raise RuntimeError("Language Model Provider is closed")
        try:
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                "max_tokens": request.max_output_tokens,
                "temperature": 0,
                "stream": False,
            }
            if request.json_mode:
                payload["response_format"] = {"type": "json_object"}
            response = await self._client.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            self._health = ProviderHealth.UNAVAILABLE
            raise AppError(
                ErrorCode.LLM_UNAVAILABLE,
                "The OpenAI-compatible language model is unavailable.",
            ) from error
        if response.status_code == 429 or response.status_code >= 500:
            self._health = ProviderHealth.UNAVAILABLE
            raise AppError(
                ErrorCode.LLM_UNAVAILABLE,
                "The OpenAI-compatible language model is temporarily unavailable.",
                {"status_code": response.status_code},
            )
        if response.status_code >= 400:
            self._health = ProviderHealth.UNAVAILABLE
            raise AppError(
                ErrorCode.LLM_UNAVAILABLE,
                "The OpenAI-compatible language model rejected the request.",
                {"status_code": response.status_code},
            )
        try:
            result = self._parse_response(response)
        except AppError:
            self._health = ProviderHealth.DEGRADED
            raise
        self._health = ProviderHealth.HEALTHY
        return result

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            await self._client.aclose()
        self._closed = True

    @staticmethod
    def _parse_response(response: httpx.Response) -> CompletionResult:
        try:
            payload: Any = response.json()
            choices = payload["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise TypeError("choices must contain one entry")
            message = choices[0]["message"]
            text = message["content"]
            usage = payload["usage"]
            input_tokens = usage["prompt_tokens"]
            output_tokens = usage["completion_tokens"]
            if not isinstance(text, str):
                raise TypeError("completion content is invalid")
            text = _visible_answer(text)
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (input_tokens, output_tokens)
            ):
                raise TypeError("completion usage is invalid")
        except (KeyError, TypeError, ValueError) as error:
            raise AppError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "The OpenAI-compatible language model returned an invalid response.",
            ) from error
        return CompletionResult(text.strip(), input_tokens, output_tokens)


def _visible_answer(text: str) -> str:
    """Remove provider presentation wrappers without altering the final answer."""

    stripped = text.strip()
    while stripped.lower().startswith("<think>"):
        visible = _THINK_BLOCK.sub("", stripped, count=1)
        if visible == stripped:
            raise TypeError("completion reasoning block is incomplete")
        stripped = visible.strip()
    fenced = _JSON_FENCE.fullmatch(stripped)
    if fenced is not None:
        stripped = fenced.group("body").strip()
    if not stripped:
        raise TypeError("completion content is invalid")
    return stripped
