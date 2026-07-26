import sqlite3

import requests

from ai.ai_provider import AIProvider
from ai.database_reader import DB_PATH, get_machine_history
from ai.equipment_knowledge import EquipmentKnowledge
from ai.hybrid_router import HybridRouter
from ai.observation_engine import (
    ObservationEngine,
    format_observations,
)
from ai.prompt_builder import build_prompt
from ai.router import MEASUREMENT_ALIASES
from ai.rule_engine import RuleEngine, format_rule_results
from ai.semantic_router import SemanticRouter
from ai.trend_analyzer import analyse_history


def create_ai_components():
    provider = AIProvider()

    equipment_knowledge = EquipmentKnowledge()

    allowed_equipment = list(
        equipment_knowledge.get_all().keys()
    )

    allowed_measurements = list(
        MEASUREMENT_ALIASES.keys()
    )

    semantic_router = SemanticRouter(
        ai=provider,
        allowed_equipment=allowed_equipment,
        allowed_measurements=allowed_measurements,
    )

    hybrid_router = HybridRouter(
        semantic_router
    )

    return (
        provider,
        hybrid_router,
        equipment_knowledge,
    )


def main():
    observation_engine = ObservationEngine()
    rule_engine = RuleEngine()

    try:
        (
            ai,
            router,
            equipment_knowledge,
        ) = create_ai_components()

    except Exception as error:
        print(
            "Unable to initialize AI components: "
            f"{error}"
        )
        return

    print("MMG Smart Machine AI")
    print(f"Database: {DB_PATH}")
    print(
        "Equipment knowledge source: "
        f"{equipment_knowledge.get_source()}"
    )
    print("Question routing: enabled")
    print(f"AI Provider: {ai.provider}")
    print("Type 'exit' to quit.")

    while True:
        try:
            question = input(
                "\nYou: "
            ).strip()

        except KeyboardInterrupt:
            print("\nAssistant stopped.")
            break

        if question.lower() in {
            "exit",
            "quit",
        }:
            print("Assistant stopped.")
            break

        if not question:
            continue

        try:
            route = router.route(
                question
            )

            print(
                f"[Router] "
                f"{route['route_source']} -> "
                f"{route['equipment']} "
                f"{route['measurements']}"
            )

            print(
                f"[Route: "
                f"{route['equipment']} | "
                f"Intent: "
                f"{route['intent']} | "
                f"History: "
                f"{route['history_limit']}]"
            )

            history = get_machine_history(
                tags=route["tags"],
                limit=route["history_limit"],
            )

            summaries = analyse_history(
                history
            )

            rule_results = rule_engine.evaluate(
                summaries
            )

            rule_context = format_rule_results(
                rule_results,
                include_normal=False,
            )

            observations = observation_engine.build(
                summaries=summaries,
                intent=route["intent"],
            )

            machine_context = format_observations(
                observations
            )

            prompt = build_prompt(
                question=question,
                machine_context=machine_context,
                rule_context=rule_context,
                route=route,
            )

            print(
                "\nAI: ",
                end="",
                flush=True,
            )

            for text in ai.stream(
                prompt
            ):
                print(
                    text,
                    end="",
                    flush=True,
                )

            print()

        except sqlite3.Error as error:
            print(
                f"\nDatabase error: {error}"
            )

        except requests.Timeout:
            print(
                "\nAI provider timed out while "
                "generating the answer."
            )

        except requests.RequestException as error:
            print(
                "\nAI provider connection error: "
                f"{error}"
            )

        except KeyError as error:
            print(
                f"\nMissing data field: {error}"
            )

        except ValueError as error:
            print(
                "\nData processing error: "
                f"{error}"
            )

        except Exception as error:
            print(
                f"\nUnexpected error: {error}"
            )


if __name__ == "__main__":
    main()
