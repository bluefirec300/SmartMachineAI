from ai.ai_provider import AIProvider
from ai.project_context import build_project_context


SYSTEM_INSTRUCTION = """
You are the software engineering assistant for SmartMachineAI.

Your job is to help develop, review, debug, and improve this industrial AI project.

Rules:
1. Base your answer on the supplied project files.
2. Do not invent files, functions, database tables, or PLC addresses.
3. Clearly identify which files should be changed.
4. When proposing code changes, provide complete replacement code when practical.
5. Preserve the current architecture unless a change is justified.
6. Protect secrets and never request API keys.
7. Treat PLC writes and machine control as safety-critical.
8. Do not generate automatic PLC write commands unless explicitly requested.
9. Prefer small, testable changes.
10. Include a test procedure after code changes.
""".strip()


def build_development_prompt(request):
    project_context = build_project_context()

    return f"""
{SYSTEM_INSTRUCTION}

===== CURRENT PROJECT FILES =====

{project_context}

===== DEVELOPMENT REQUEST =====

{request}

===== RESPONSE REQUIREMENTS =====

Review the existing project before answering.

Provide:
- your understanding of the request
- affected files
- recommended implementation
- complete code where appropriate
- commands to test the change
- risks or compatibility concerns
""".strip()


def main():
    print("SmartMachineAI Development Assistant")
    print("Provider: loading...")
    print("Type 'exit' to quit.")

    ai = AIProvider()

    print(f"Provider: {ai.provider}")

    while True:
        try:
            request = input("\nDeveloper request: ").strip()
        except KeyboardInterrupt:
            print("\nDevelopment assistant stopped.")
            break

        if request.lower() in {"exit", "quit"}:
            print("Development assistant stopped.")
            break

        if not request:
            continue

        prompt = build_development_prompt(request)

        print("\nAssistant:\n")

        try:
            for text in ai.stream(prompt):
                print(text, end="", flush=True)

            print()

        except Exception as error:
            print(f"\nDevelopment assistant error: {error}")


if __name__ == "__main__":
    main()
