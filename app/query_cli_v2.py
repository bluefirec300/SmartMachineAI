from __future__ import annotations
import argparse, json
from config.environment import get_config_db_path
from engine import IndustrialQueryEngine

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--database", default=str(get_config_db_path()))
    args = p.parse_args()
    engine = IndustrialQueryEngine(args.database)

    print("SmartMachineAI v2 deterministic query engine")
    print("Type 'exit' to stop.")
    while True:
        try:
            question = input("\nQuestion: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in {"exit", "quit"}:
            break
        if not question:
            continue
        try:
            result = engine.query(question)
        except Exception as exc:
            print(f"Query error: {exc}")
            continue

        print(f"\nStatus: {result.status}")
        print(f"Intent: {result.intent}")
        print(f"Selected tag: {result.selected_tag or 'None'}")
        print(f"Equipment: {result.equipment or 'None'}")
        print(f"Measurement: {result.measurement or 'None'}")
        print(f"Location: {result.location or 'None'}")
        print(f"Condition: {result.condition or 'None'}")
        print(f"Time: {result.time_expression or 'None'}")
        print(f"Confidence: {result.confidence:.1%}")
        print(f"Elapsed: {result.elapsed_seconds:.6f} seconds")
        print(f"Message: {result.message}")
        print("\nCandidates:")
        for c in result.candidates:
            print(f"  {c.tag.tag_name:<28} score={c.score:>7.3f}  {'; '.join(c.reasons)}")
        print("\nJSON:")
        print(json.dumps(result.to_dict(), indent=2))

if __name__ == "__main__":
    main()
