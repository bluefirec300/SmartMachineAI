import os

from ai.providers.base_provider import BaseAIProvider
from ai.providers.ollama_provider import OllamaProvider
from ai.providers.openai_provider import OpenAIProvider
from config.config_manager import ConfigManager


class ProviderFactory:
    """
    Creates the configured AI provider.

    Configuration priority:

    1. Environment variables
    2. config/settings.ini
    3. Safe built-in defaults
    """

    @staticmethod
    def _environment_value(
        name: str,
    ) -> str | None:
        value = os.getenv(name)

        if value is None:
            return None

        value = value.strip()

        return value or None

    @classmethod
    def get_provider_name(
        cls,
        config: ConfigManager,
    ) -> str:
        provider = (
            cls._environment_value("AI_PROVIDER")
            or config.ai_provider
            or "ollama"
        )

        return provider.strip().lower()

    @classmethod
    def create(
        cls,
        provider_name: str | None = None,
    ) -> BaseAIProvider:
        config = ConfigManager()

        selected_provider = (
            provider_name
            or cls.get_provider_name(config)
        ).strip().lower()

        if selected_provider == "ollama":
            base_url = (
                cls._environment_value("OLLAMA_URL")
                or config.ollama_url
                or "http://localhost:11434"
            )

            model = (
                cls._environment_value("OLLAMA_MODEL")
                or config.ollama_model
                or "qwen2.5:7b"
            )

            return OllamaProvider(
                base_url=base_url,
                model=model,
            )

        if selected_provider == "openai":
            model = (
                cls._environment_value("OPENAI_MODEL")
                or config.ai_model
                or "gpt-5"
            )

            return OpenAIProvider(
                model=model
            )

        raise ValueError(
            f"Unsupported AI provider: "
            f"{selected_provider}. "
            "Supported providers are "
            "'ollama' and 'openai'."
        )
