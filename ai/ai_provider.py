import os

from ollama_client import stream_ollama
from openai_client import OpenAIClient


class AIProvider:
    """Selects and uses either OpenAI or Ollama."""

    def __init__(self):
        self.provider = os.getenv(
            "AI_PROVIDER",
            "ollama",
        ).strip().lower()

        self.client = None

        if self.provider == "openai":
            self.client = OpenAIClient()

        elif self.provider == "ollama":
            pass

        else:
            raise ValueError(
                f"Unsupported AI_PROVIDER: {self.provider}. "
                "Use 'openai' or 'ollama'."
            )

    def generate(self, prompt):
        """Generate one complete response."""

        if not prompt or not prompt.strip():
            return "No prompt was provided."

        if self.provider == "ollama":
            return "".join(stream_ollama(prompt))

        return self.client.generate(prompt)

    def stream(self, prompt):
        """Yield response text in chunks."""

        if not prompt or not prompt.strip():
            yield "No prompt was provided."
            return

        if self.provider == "ollama":
            yield from stream_ollama(prompt)
            return

        answer = self.client.generate(prompt)
        yield answer
