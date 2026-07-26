import sqlite3

import requests

from ai_provider import AIProvider
from database_reader import DB_PATH, get_machine_history
from prompt_builder import build_prompt, format_machine_context
from router import route_question
from trend_analyzer import analyse_history


def main():
    print("MMG Smart Machine AI")
    print(f"Database: {DB_PATH}")
    print("Question routing: enabled")

    try:
        ai = AIProvider()
    except Exception as error:
        print(f"Unable to initialize AI provider: {error}")
        return

    print(f"AI Provider: {ai.provider}")
    print("Type 'exit' to quit.")

    while True:
        try:
            question = input("\nYou: ").strip()
        except KeyboardInterrupt:
            print("\nAssistant stopped.")
            break

        if question.lower() in {"exit", "quit"}:
            print("Assistant stopped.")
            break

        if not question:
            continue

        try:
            route = route_question(question)

            print(
                f"[Route: {route['equipment']} | "
                f"Intent: {route['intent']} | "
                f"History: {route['history_limit']}]"
            )

            history = get_machine_history(
                tags=route["tags"],
                limit=route["history_limit"],
            )

            summaries = analyse_history(history)

            context = format_machine_context(
                summaries=summaries,
                intent=route["intent"],
            )

            prompt = build_prompt(
                question=question,
                machine_context=context,
                route=route,
            )

            print("\nAI: ", end="", flush=True)

            for text in ai.stream(prompt):
                print(text, end="", flush=True)

            print()

        except sqlite3.Error as error:
            print(f"\nDatabase error: {error}")

        except requests.Timeout:
            print("\nAI provider timed out while generating the answer.")

        except requests.RequestException as error:
            print(f"\nAI provider connection error: {error}")

        except KeyError as error:
            print(f"\nMissing data field: {error}")

        except ValueError as error:
            print(f"\nData processing error: {error}")

        except Exception as error:
            print(f"\nUnexpected error: {error}")


if __name__ == "__main__":
    main()
