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


def build_prompt(
    question,
    machine_context,
    rule_context,
    route,
):
    if route["intent"] == "root_cause":
        structure_instructions = (
            "Clearly separate:\n"
            "1. Confirmed observations.\n"
            "2. Possible engineering causes.\n"
            "3. Recommended checks.\n\n"
            'Base "Possible engineering causes" only on the supplied '
            "root-cause evidence and probable contributing "
            "conditions. Do not add a cause that isn't supported by "
            "the supplied evidence.\n\n"
            "If manufacturer documentation excerpts are supplied "
            "below, prefer citing their specific guidance in "
            '"Recommended checks" over generic advice - but only '
            "what the excerpts actually say. Do not invent a "
            "procedure or specification that isn't in the supplied "
            "excerpts."
        )
    elif route["intent"] == "comparison":
        structure_instructions = (
            "Answer using only the confirmed observations below.\n\n"
            "The comparison verdict (which period is higher or lower, "
            "and by how much) has already been computed exactly in "
            "code and is stated below as a CONFIRMED FACT. Restate it "
            "in plain language only - do not recompute it, do not "
            "second-guess it, and do not reverse which period you "
            "call higher or lower."
        )
    else:
        structure_instructions = (
            "Answer using only the confirmed observations below.\n\n"
            'Do not add a "possible causes" or "recommended checks" '
            'section - the operator did not ask "why," so do not '
            "speculate about causes."
        )

    return f"""
You are an industrial machine assistant.

Selected equipment:
{route['equipment']}

Question intent:
{route['intent']}

Use only the supplied observations when stating current values,
historical values, trends, alarms or operating states.

Do not invent sensor readings.

{structure_instructions}

For a simple current-value question, answer directly and briefly.
For a status or trend question, consider all supplied related tags.

Do not repeat the same operating state more than once.
Do not repeat an observation using different wording.

Engineering limit evaluation:
{rule_context}

Confirmed engineering observations:
{machine_context}

Operator question:
{question}

Give a clear and concise answer.
Treat engineering warnings and alarms as deterministic results.
Do not contradict the supplied engineering limit evaluation.
""".strip()


