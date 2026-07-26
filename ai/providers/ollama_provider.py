import json
from collections.abc import Iterator

import requests

from ai.providers.base_provider import BaseAIProvider


class OllamaProvider(BaseAIProvider):
    """
    Local Ollama AI provider.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        connect_timeout: int = 10,
        read_timeout: int = 300,
    ):
        if not base_url.strip():
            raise ValueError(
                "Ollama base URL cannot be empty."
            )

        if not model.strip():
            raise ValueError(
                "Ollama model cannot be empty."
            )

        self.base_url = base_url.rstrip("/")
        self._model = model.strip()
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    @property
    def generate_url(self) -> str:
        return f"{self.base_url}/api/generate"

    def _payload(
        self,
        prompt: str,
        stream: bool,
    ) -> dict:
        return {
            "model": self.model,
            "prompt": prompt,
            "stream": stream,
            "keep_alive": "10m",
            "options": {
                "num_predict": 200,
                "temperature": 0.1,
                "num_ctx": 2048,
            },
        }

    def generate(self, prompt: str) -> str:
        if not prompt or not prompt.strip():
            return "No prompt was provided."

        response = requests.post(
            self.generate_url,
            json=self._payload(
                prompt=prompt,
                stream=False,
            ),
            timeout=(
                self.connect_timeout,
                self.read_timeout,
            ),
        )

        response.raise_for_status()

        result = response.json()
        text = result.get("response", "")

        if not text:
            return "[Ollama returned no text.]"

        return str(text).strip()

    def stream(
        self,
        prompt: str,
    ) -> Iterator[str]:
        if not prompt or not prompt.strip():
            yield "No prompt was provided."
            return

        response = requests.post(
            self.generate_url,
            json=self._payload(
                prompt=prompt,
                stream=True,
            ),
            stream=True,
            timeout=(
                self.connect_timeout,
                self.read_timeout,
            ),
        )

        response.raise_for_status()

        received_text = False

        for line in response.iter_lines(
            chunk_size=1,
            decode_unicode=True,
        ):
            if not line:
                continue

            result = json.loads(line)

            text = result.get("response", "")

            if text:
                received_text = True
                yield str(text)

            if result.get("done", False):
                break

        if not received_text:
            yield "[Ollama returned no text.]"
