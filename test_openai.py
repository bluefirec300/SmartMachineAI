import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

response = client.responses.create(
    model="gpt-5",
    input="Reply only with: SmartMachineAI API is working."
)

print(response.output_text)
