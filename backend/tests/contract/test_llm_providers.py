import json

import httpx
import pytest

from enterprise_rag.adapters.llms import OpenAICompatibleLanguageModel
from enterprise_rag.domain import AppError, ErrorCode
from enterprise_rag.ports import CompletionRequest, ProviderHealth


def request() -> CompletionRequest:
    return CompletionRequest("Use evidence only.", "Question and evidence", 256)


@pytest.mark.anyio
async def test_openai_compatible_llm_sends_chat_request_and_parses_usage() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url == "https://provider.example/v1/chat/completions"
        assert incoming.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(incoming.content)
        assert payload == {
            "model": "chat-model",
            "messages": [
                {"role": "system", "content": "Use evidence only."},
                {"role": "user", "content": "Question and evidence"},
            ],
            "max_tokens": 256,
            "temperature": 0,
            "stream": False,
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "<think>private reasoning</think>\n Answer [1]. ",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleLanguageModel(
            base_url="https://provider.example/v1/",
            api_key="test-secret",
            model="chat-model",
            client=client,
        )
        result = await provider.complete(request())

    assert result.text == "Answer [1]."
    assert result.input_tokens == 12
    assert result.output_tokens == 4
    assert provider.info().health is ProviderHealth.HEALTHY


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (401, {"error": "sensitive upstream body"}, ErrorCode.LLM_UNAVAILABLE),
        (500, {"error": "sensitive upstream body"}, ErrorCode.LLM_UNAVAILABLE),
        (200, {"choices": [], "usage": {}}, ErrorCode.LLM_INVALID_RESPONSE),
        (
            200,
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": "12", "completion_tokens": 4},
            },
            ErrorCode.LLM_INVALID_RESPONSE,
        ),
        (
            200,
            {
                "choices": [{"message": {"content": "<think>unfinished"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
            ErrorCode.LLM_INVALID_RESPONSE,
        ),
    ],
)
async def test_openai_compatible_llm_rejects_errors_without_leaking_body(
    status: int, payload: object, code: ErrorCode
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    ) as client:
        provider = OpenAICompatibleLanguageModel(
            base_url="https://provider.example/v1",
            api_key="not-in-errors",
            model="chat-model",
            client=client,
        )
        with pytest.raises(AppError) as raised:
            await provider.complete(request())

    assert raised.value.code is code
    assert "sensitive upstream body" not in str(raised.value)
    assert "not-in-errors" not in str(raised.value)


@pytest.mark.anyio
async def test_openai_compatible_llm_close_is_idempotent() -> None:
    provider = OpenAICompatibleLanguageModel(
        base_url="https://provider.example/v1",
        api_key="secret",
        model="chat-model",
    )
    await provider.aclose()
    await provider.aclose()
    assert provider.info().health is ProviderHealth.UNAVAILABLE
    with pytest.raises(RuntimeError, match="closed"):
        await provider.complete(request())
