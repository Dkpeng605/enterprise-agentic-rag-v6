"""Minimal text-generation contract shared by query graphs."""

from dataclasses import dataclass
from typing import Protocol

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.ports.provider import Provider


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    system_prompt: str
    user_prompt: str
    max_output_tokens: int

    def __post_init__(self) -> None:
        require_non_empty(self.system_prompt, "system_prompt")
        require_non_empty(self.user_prompt, "user_prompt")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True, slots=True)
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        require_non_empty(self.text, "text")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token counts must not be negative")


class LanguageModel(Provider, Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...
