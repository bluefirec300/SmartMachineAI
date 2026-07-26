from ai.trend_analyzer import format_number


def format_machine_context(summaries, intent):
    if not summaries:
        return "No matching machine data is available."

    sections = []

    for summary in summaries:
        unit = summary["unit"]

        lines = [
            f"Tag: {summary['tag']}",
            f"PLC address: {summary['address']}",
            f"Updated: {summary['updated']}",
        ]

        if unit:
            lines.append(
                f"Current value: "
                f"{format_number(summary['current'])} {unit}"
            )
        else:
            lines.append(f"Current value: {summary['current']}")

        if intent != "current":
            lines.append(f"Samples analysed: {summary['samples']}")
            lines.append(f"Trend: {summary['trend']}")

            if summary["average"] is not None:
                lines.extend(
                    [
                        f"Average: "
                        f"{format_number(summary['average'])} {unit}",
                        f"Minimum: "
                        f"{format_number(summary['minimum'])} {unit}",
                        f"Maximum: "
                        f"{format_number(summary['maximum'])} {unit}",
                        f"Change: "
                        f"{format_number(summary['change'])} {unit}",
                    ]
                )

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def build_prompt(question, machine_context, route):
    return f"""
You are an industrial machine assistant.

Selected equipment:
{route['equipment']}

Question intent:
{route['intent']}

Use only the supplied machine data when stating current values,
historical values, trends, alarms or operating states.

Do not invent sensor readings.

Clearly separate:
1. Confirmed observations.
2. Possible engineering causes.
3. Recommended checks.

For a simple current-value question, answer directly and briefly.
For a status or trend question, consider all supplied related tags.

Machine data:
{machine_context}

Operator question:
{question}

Give a clear and concise answer.
""".strip()
