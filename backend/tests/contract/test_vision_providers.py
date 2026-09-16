"""Contract tests for OpenAI-compatible image captioning Providers."""

import base64
import json

import httpx
import pytest

from enterprise_rag.adapters.vision import OpenAICompatibleVisionProvider
from enterprise_rag.domain import AppError, ErrorCode
from enterprise_rag.ports import VisionImage

IMAGE_BYTES = b"fake-png-payload"
IMAGE = VisionImage(
    name="diagram.png",
    media_type="image/png",
    sha256="a" * 64,
    width=320,
    height=200,
    data=IMAGE_BYTES,
)


@pytest.fixture
def anyio_backend() -> str:
    """Run this contract with the backend shipped by the project test image."""

    return "asyncio"


def _caption_response(content: object = "A diagram of the retrieval pipeline.") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 9},
        },
    )


@pytest.mark.anyio
async def test_http_provider_sends_openai_multimodal_data_uri_and_returns_caption() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://vision.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.read())
        assert payload == {
            "model": "vision-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Convert this image into concise, searchable text for an "
                                "enterprise knowledge base. Transcribe visible text, numbers, "
                                "code, tables, labels, and relationships accurately; describe "
                                "diagrams or charts when needed. Return only the final "
                                "description, without reasoning, preambles, or Markdown fences."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(IMAGE_BYTES).decode(),
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 256,
            "temperature": 0,
            "stream": False,
        }
        return _caption_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleVisionProvider(
            base_url="https://vision.example/v1",
            api_key="test-secret",
            model="vision-model",
            client=client,
        )
        caption = await provider.caption(IMAGE)

    assert caption == "A diagram of the retrieval pipeline."
    assert provider.info().name == "openai_compatible"
    assert provider.info().version == "vision-model"
    assert provider.info().is_remote is True


@pytest.mark.anyio
async def test_http_provider_removes_minimax_reasoning_before_persisting_caption() -> None:
    response = _caption_response(
        '<think>private reasoning must not enter retrieval</think>\n\n'
        "图中有一个带 RAG 标签的流程框。"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleVisionProvider(
            base_url="https://vision.example/v1",
            api_key="test-secret",
            model="MiniMax-M3",
            client=client,
        )
        caption = await provider.caption(IMAGE)

    assert caption == "图中有一个带 RAG 标签的流程框。"
    assert "private reasoning" not in caption


@pytest.mark.anyio
async def test_http_provider_retries_429_and_5xx_with_bounded_backoff() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "secret rate limit"})
        if calls == 2:
            return httpx.Response(503, json={"error": "secret outage"})
        return _caption_response("caption")

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleVisionProvider(
            base_url="https://vision.example/v1",
            api_key="not-in-errors",
            model="vision-model",
            max_retries=2,
            client=client,
            sleeper=sleeper,
        )
        assert await provider.caption(IMAGE) == "caption"

    assert calls == 3
    assert sleeps == [0.25, 0.5]


@pytest.mark.anyio
async def test_http_provider_sanitizes_transport_failure_and_bounds_retries() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.RemoteProtocolError("secret upstream diagnostic", request=request)

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleVisionProvider(
            base_url="https://vision.example/v1",
            api_key="secret-api-key",
            model="vision-model",
            max_retries=1,
            client=client,
            sleeper=sleeper,
        )
        with pytest.raises(AppError) as raised:
            await provider.caption(IMAGE)

    assert calls == 2
    assert sleeps == [0.25]
    assert raised.value.code is ErrorCode.VISION_UNAVAILABLE
    assert "secret upstream diagnostic" not in str(raised.value)
    assert "secret-api-key" not in str(raised.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": ["caption"]}}]}),
        httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "<think>unfinished reasoning"}}
                ]
            },
        ),
        httpx.Response(200, content=b"not-json"),
    ],
)
async def test_http_provider_rejects_empty_or_malformed_caption_without_leaking_body(
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleVisionProvider(
            base_url="https://vision.example/v1",
            api_key="not-in-errors",
            model="vision-model",
            client=client,
        )
        with pytest.raises(AppError) as raised:
            await provider.caption(IMAGE)

    assert raised.value.code is ErrorCode.VISION_INVALID_RESPONSE
    assert "not-json" not in str(raised.value)
    assert "not-in-errors" not in str(raised.value)


@pytest.mark.anyio
async def test_http_provider_rejects_non_image_and_oversized_input_before_network() -> None:
    non_image = VisionImage(
        name="document.txt",
        media_type="text/plain",
        sha256="b" * 64,
        width=1,
        height=1,
        data=b"text",
    )

    async def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the network must not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        provider = OpenAICompatibleVisionProvider(
            base_url="https://vision.example/v1",
            api_key="test-secret",
            model="vision-model",
            max_image_bytes=3,
            client=client,
        )
        with pytest.raises(AppError) as raised:
            await provider.caption(non_image)
    assert raised.value.code is ErrorCode.VISION_INPUT_INVALID

    with pytest.raises(AppError) as raised:
        await provider.caption(IMAGE)
    assert raised.value.code is ErrorCode.VISION_INPUT_INVALID


@pytest.mark.anyio
async def test_http_provider_close_is_idempotent() -> None:
    provider = OpenAICompatibleVisionProvider(
        base_url="https://vision.example/v1",
        api_key="test-secret",
        model="vision-model",
    )
    await provider.aclose()
    await provider.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await provider.caption(IMAGE)
