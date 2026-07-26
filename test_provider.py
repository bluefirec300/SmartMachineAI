from ai.ai_provider import AIProvider

ai = AIProvider()

print(f"Provider: {ai.provider}")
print()

for chunk in ai.stream(
    "Reply only with: AI Provider Manager is working."
):
    print(chunk, end="", flush=True)

print()
