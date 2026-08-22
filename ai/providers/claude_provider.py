import os
from collections.abc import Iterator

from anthropic import (
    APIConnectionError,
    AuthenticationError,
    Anthropic,
    RateLimitError,
)

from ai.providers.base_provider import BaseAIProvider

DEFAULT_MAX_TOKENS = 2048


class ClaudeProvider(BaseAIProvider):
    """
    (testing) - Anthropic Claude provider, added to try Claude as a
    third option alongside OpenAI/Ollama for this app's own
    deterministic-facts-only prompts. Not wired into any default path -
    settings.ini's [AI] provider stays "ollama" (the production
    choice) unless explicitly switched. `name` returns "claude
    (testing)" specifically so this stays visibly marked wherever a
    provider name is shown (e.g. Ask AI's "AI provider: ..." caption),
    the same way an engineer would want to know at a glance that an
    answer came from an experimental path, not the tuned production one.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        resolved_key = (
            api_key
            or os.getenv("ANTHROPIC_API_KEY")
        )

        if not resolved_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not loaded. "
                "Run: source ~/.anthropic_env "
                "(create it with: echo 'export ANTHROPIC_API_KEY=sk-ant-...' > ~/.anthropic_env)"
            )

        if not model.strip():
            raise ValueError(
                "Claude model cannot be empty."
            )

        self._model = model.strip()
        self._max_tokens = max_tokens
        self.client = Anthropic(
            api_key=resolved_key
        )

    @property
    def name(self) -> str:
        return "claude (testing)"

    @property
    def model(self) -> str:
        return self._model

    def generate(self, prompt: str) -> str:
        if not prompt or not prompt.strip():
            return "No prompt was provided."

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )

            text = "".join(
                block.text
                for block in response.content
                if block.type == "text"
            )

            if not text:
                return "[Claude returned no text.]"

            return text.strip()

        except AuthenticationError:
            return (
                "Claude authentication failed. "
                "Check ANTHROPIC_API_KEY."
            )

        except RateLimitError:
            return (
                "Claude quota or rate limit exceeded."
            )

        except APIConnectionError:
            return "Unable to connect to Claude."

        except Exception as error:
            return f"Claude error: {error}"

    def stream(
        self,
        prompt: str,
    ) -> Iterator[str]:
        """
        Same non-streaming shim as OpenAIProvider.stream() - one
        complete response as a single chunk. True token streaming can
        be added later without changing AIProvider or its callers.
        """
        yield self.generate(prompt)
