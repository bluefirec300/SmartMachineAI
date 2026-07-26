import os
from collections.abc import Iterator

from openai import (
    APIConnectionError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from ai.providers.base_provider import BaseAIProvider


class OpenAIProvider(BaseAIProvider):
    """
    Cloud OpenAI provider.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
    ):
        resolved_key = (
            api_key
            or os.getenv("OPENAI_API_KEY")
        )

        if not resolved_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not loaded. "
                "Run: source ~/.openai_env"
            )

        if not model.strip():
            raise ValueError(
                "OpenAI model cannot be empty."
            )

        self._model = model.strip()
        self.client = OpenAI(
            api_key=resolved_key
        )

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model

    def generate(self, prompt: str) -> str:
        if not prompt or not prompt.strip():
            return "No prompt was provided."

        try:
            response = self.client.responses.create(
                model=self.model,
                input=prompt,
            )

            text = response.output_text

            if not text:
                return "[OpenAI returned no text.]"

            return text.strip()

        except AuthenticationError:
            return (
                "OpenAI authentication failed. "
                "Check the API key."
            )

        except RateLimitError:
            return (
                "OpenAI quota or rate limit exceeded."
            )

        except APIConnectionError:
            return "Unable to connect to OpenAI."

        except Exception as error:
            return f"OpenAI error: {error}"

    def stream(
        self,
        prompt: str,
    ) -> Iterator[str]:
        """
        The current OpenAI implementation returns one complete
        response as a single stream chunk.

        True token streaming can be added later without changing
        AIProvider or its callers.
        """
        yield self.generate(prompt)
