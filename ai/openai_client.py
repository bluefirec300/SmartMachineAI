import os

from openai import (
    APIConnectionError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)


class OpenAIClient:
    """Reusable OpenAI provider for SmartMachineAI."""

    def __init__(self, model: str = "gpt-5"):
        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not loaded. Run: source ~/.openai_env"
            )

        self.model = model
        self.client = OpenAI(api_key=api_key)

    def generate(self, prompt: str) -> str:
        if not prompt.strip():
            return "No prompt was provided."

        try:
            response = self.client.responses.create(
                model=self.model,
                input=prompt,
            )

            return response.output_text.strip()

        except AuthenticationError:
            return "OpenAI authentication failed. Check the API key."

        except RateLimitError:
            return "OpenAI quota or rate limit exceeded."

        except APIConnectionError:
            return "Unable to connect to OpenAI."

        except Exception as error:
            return f"OpenAI error: {error}"


if __name__ == "__main__":
    ai = OpenAIClient()

    answer = ai.generate(
        "Reply only with: OpenAI client module is working."
    )

    print(answer)
