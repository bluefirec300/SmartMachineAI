from collections.abc import Iterator

from ai.providers.base_provider import BaseAIProvider
from ai.providers.provider_factory import ProviderFactory


class AIProvider:
    """
    Backward-compatible facade for AI providers.

    Existing callers can continue using:

        ai = AIProvider()
        ai.provider
        ai.model
        ai.generate(prompt)
        ai.stream(prompt)
    """

    def __init__(
        self,
        provider_name: str | None = None,
        client: BaseAIProvider | None = None,
    ):
        self.client = (
            client
            or ProviderFactory.create(
                provider_name=provider_name
            )
        )

    @property
    def provider(self) -> str:
        return self.client.name

    @property
    def model(self) -> str:
        return self.client.model

    def generate(
        self,
        prompt: str,
    ) -> str:
        return self.client.generate(prompt)

    def stream(
        self,
        prompt: str,
    ) -> Iterator[str]:
        yield from self.client.stream(prompt)
