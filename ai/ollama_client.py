import json

import requests


MODEL_NAME = "qwen2.5:3b"
OLLAMA_URL = "http://localhost:11434/api/generate"


def stream_ollama(prompt):
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL_NAME,
            "prompt": prompt,
            "stream": True,
            "keep_alive": "10m",
            "options": {
                "num_predict": 100,
                "temperature": 0.1,
                "num_ctx": 2048,
            },
        },
        stream=True,
        timeout=(10, 300),
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
            yield text

        if result.get("done", False):
            break

    if not received_text:
        yield "[Ollama returned no text.]"
