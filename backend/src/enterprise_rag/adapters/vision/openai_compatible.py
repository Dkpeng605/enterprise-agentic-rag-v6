"""OpenAI-compatible multimodal captioning adapter."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.vision import VisionImage

Sleeper = Callable[[float], Awaitable[None]]


class VisionError(AppError):
    """A client-safe Vision Provider failure."""


class OpenAICompatibleVisionProvider:
    """Caption images through an OpenAI-compatible Chat Completions endpoint.

    The adapter intentionally sends a single non-streaming multimodal request.
    Retry policy is bounded and only applies to transport failures, HTTP 429, and
    server-side errors. Provider response bodies are never included in errors.
    """

    _CAPTION_PROMPT = "Describe the visible content of this image for enterprise retrieval."

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        provider_name: str = "openai_compatible",
        max_image_bytes: int = 10 * 1024 * 1024,
        max_output_tokens: int = 256,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if (
            not base_url.strip()
            or not api_key.strip()
            or not model.strip()
            or not provider_name.strip()
        ):
            raise ValueError("OpenAI-compatible Vision configuration must not be blank")
        if max_image_bytes <= 0 or max_output_tokens <= 0:
            raise ValueError("Vision image and output limits must be positive")
        if timeout_seconds <= 0 or not 0 <= max_retries <= 10:
            raise ValueError("Vision timeout and retry settings are invalid")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._provider_name = provider_name
        self._max_image_bytes = max_image_bytes
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._sleeper = sleeper
        self._health = ProviderHealth.UNKNOWN
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.VISION,
            name=self._provider_name,
            version=self._model,
            capabilities=frozenset({"image-caption", "chat-completions", "http", "retry"}),
            is_remote=True,
            health=ProviderHealth.UNAVAILABLE if self._closed else self._health,
        )

    async def caption(self, image: VisionImage) -> str | None:
        if self._closed:
            raise RuntimeError("Vision Provider is closed")
        self._validate_image(image)
        encoded = base64.b64encode(image.data).decode("ascii")
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._CAPTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.media_type};base64,{encoded}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": 0,
            "stream": False,
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self._timeout,
                )
            except httpx.TransportError as error:
                self._health = ProviderHealth.UNAVAILABLE
                if attempt >= self._max_retries:
                    raise VisionError(
                        ErrorCode.VISION_UNAVAILABLE,
                        "The remote Vision Provider is unavailable.",
                    ) from error
                await self._sleeper(0.25 * (2**attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                self._health = ProviderHealth.UNAVAILABLE
                if attempt < self._max_retries:
                    await self._sleeper(0.25 * (2**attempt))
                    continue
                raise VisionError(
                    ErrorCode.VISION_UNAVAILABLE,
                    "The remote Vision Provider exhausted bounded retries.",
                    {"status_code": response.status_code},
                )
            if response.status_code >= 400:
                self._health = ProviderHealth.UNAVAILABLE
                raise VisionError(
                    ErrorCode.VISION_UNAVAILABLE,
                    "The remote Vision Provider rejected the request.",
                    {"status_code": response.status_code},
                )
            try:
                caption = self._parse_response(response)
            except VisionError:
                self._health = ProviderHealth.DEGRADED
                raise
            self._health = ProviderHealth.HEALTHY
            return caption
        raise AssertionError("retry loop did not terminate")

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            await self._client.aclose()
        self._closed = True

    def _validate_image(self, image: VisionImage) -> None:
        if not image.media_type.lower().startswith("image/"):
            raise VisionError(
                ErrorCode.VISION_INPUT_INVALID,
                "Vision input must use an image media type.",
            )
        if len(image.data) > self._max_image_bytes:
            raise VisionError(
                ErrorCode.VISION_INPUT_INVALID,
                "Vision input exceeds the configured image size limit.",
                {"max_image_bytes": self._max_image_bytes},
            )

    @staticmethod
    def _parse_response(response: httpx.Response) -> str:
        try:
            payload: Any = response.json()
            choices = payload["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise TypeError("choices must contain one entry")
            content = choices[0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise TypeError("caption content must be a non-empty string")
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise VisionError(
                ErrorCode.VISION_INVALID_RESPONSE,
                "The remote Vision Provider returned an invalid response.",
            ) from error
        return content.strip()
