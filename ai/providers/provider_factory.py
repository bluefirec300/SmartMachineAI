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
        model_override: str | None = None,
    ) -> BaseAIProvider:
        """
        model_override (Phase V2.4 - Ask AI Fast/Thorough mode): a
        specific, session-chosen model that wins over everything else
        below it - env var, settings.ini, and the built-in default -
        for whichever provider ends up selected. Left None (the
        default for every existing caller), routing is completely
        unchanged from before this phase. Deliberately NOT a new
        precedence tier documented as replacing env/settings/default -
        it's an explicit, one-off override a caller opts into, the
        same way passing an explicit client already bypasses provider
        selection entirely in AIProvider.__init__.
        """
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
                model_override
                or cls._environment_value("OLLAMA_MODEL")
                or config.ollama_model
                or "qwen2.5:7b"
            )

            # Same env-var-first pattern as base_url/model above. Left
            # unset by default so OllamaProvider's own 600s default
            # applies unchanged - this exists so a slower/faster
            # deployment can tune it (e.g. a CPU-only dev VM during
            # commissioning) without touching provider code, never as a
            # routing/architecture change.
            read_timeout_value = cls._environment_value("OLLAMA_READ_TIMEOUT")
            read_timeout_kwargs = (
                {"read_timeout": int(read_timeout_value)}
                if read_timeout_value
                else {}
            )

            return OllamaProvider(
                base_url=base_url,
                model=model,
                **read_timeout_kwargs,
            )

        if selected_provider == "openai":
            # model_override is deliberately NOT applied here - Fast/
            # Thorough mode names specific Ollama models (qwen2.5:3b/
            # 7b), which would be meaningless passed to OpenAI. A
            # caller on the openai provider gets its normal
            # env/settings/default model regardless of mode.
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
