from __future__ import annotations
import re
from .models import Concepts

MEASUREMENTS = {
    "pressure": ("pressure", "bar", "psi"),
    "temperature": ("temperature", "temp", "celsius"),
    "humidity": ("humidity", "relative humidity"),
    "current": ("motor current", "electrical current", "amps", "ampere", "amp"),
    "voltage": ("voltage", "volts"),
    "power": ("power", "kw", "kilowatt"),
    "energy": ("energy", "kwh"),
    "flow": ("flow rate", "flowrate", "flow"),
    "level": ("tank level", "level"),
    "speed": ("speed", "rpm"),
    "frequency": ("frequency", "hertz", "hz"),
    "weight": ("weight", "kilogram", "kg"),
    "running": ("run status", "running", "operating"),
    "status": ("status", "state"),
}
LOCATIONS = {
    "discharge": ("discharge", "output side"),
    "suction": ("suction", "intake"),
    "inlet": ("inlet", "incoming"),
    "outlet": ("outlet", "outgoing"),
    "supply": ("supply",),
    "return": ("return",),
    "motor": ("motor",),
    "room": ("room",),
    "tank": ("tank",),
    "bearing": ("bearing",),
    "header": ("header",),
    "incomer": ("incomer",),
    "breaker": ("breaker",),
}
CONDITIONS = {
    "high": ("too high", "high"),
    "low": ("too low", "low"),
    "running": ("running", "operating"),
    "stopped": ("not running", "stopped"),
    "open": ("opened", "open"),
    "closed": ("closed", "shut"),
    "increasing": ("increasing", "rising", "going up"),
    "decreasing": ("decreasing", "falling", "dropping"),
    "abnormal": ("abnormal", "unusual"),
}
EVENTS = {
    "alarm": ("alarms", "alarm"),
    "warning": ("warnings", "warning"),
    "fault": ("tripped", "trip", "fault", "error"),
}
TIMES = {
    "last_30_minutes": ("last 30 minutes", "past 30 minutes"),
    "last_hour": ("last hour", "past hour"),
    "last_7_days": ("last 7 days", "last week"),
    "yesterday": ("yesterday",),
    "today": ("today",),
    "recent": ("recently", "recent"),
    "now": ("right now", "now"),
    "latest": ("currently", "current", "latest"),
}
STOPWORDS = {
    "what","is","the","a","an","of","for","to","from","in","on","at",
    "show","me","please","tell","give","value","reading","currently",
    "current","latest","now","status","are","was","did","has","have",
    "why","when","where","how","does","do","will","would","can","could",
    "it","its","this","that","be","been","being",
    "trend","limit","trip",
    "there","anything","happen","happened","wrong","any",
}

def normalize(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = text.replace("_", " ").replace("-", " ").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def contains(text: str, phrase: str) -> bool:
    return f" {normalize(phrase)} " in f" {normalize(text)} "

def first_match(text: str, mapping: dict[str, tuple[str, ...]]) -> tuple[str, str]:
    for canonical, aliases in mapping.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if contains(text, alias):
                return canonical, alias
    return "", ""

class ConceptExtractor:
    def extract(self, question: str) -> Concepts:
        question = str(question or "").strip()
        if not question:
            raise ValueError("Question cannot be empty.")
        text = normalize(question)

        discovery_words = set(text.split())
        discovery_targets = {
            "component", "components", "equipment", "tag", "tags",
        }

        if (
            "available" in discovery_words
            or "monitor" in discovery_words
            or (
                discovery_words & {"list", "show"}
                and discovery_words & discovery_targets
            )
            or any(
                contains(text, p)
                for p in ("what can i check", "what can i ask", "help")
            )
        ):
            intent = "discovery"
        elif any(contains(text, p) for p in ("why did", "why is", "what caused", "root cause")):
            intent = "root_cause"
        elif any(
            contains(text, p)
            for p in (
                "alarm history", "event history", "when did", "timeline",
                "anything happen", "what happened", "any events", "any alarms",
                "anything wrong",
            )
        ):
            intent = "timeline"
        elif any(contains(text, p) for p in ("trend", "over time", "increasing", "decreasing", "history of")):
            intent = "trend"
        elif any(contains(text, p) for p in ("alarm limit", "warning limit", "threshold", "setpoint", "trip point")):
            intent = "threshold"
        else:
            intent = "current_data"

        measurement, m_alias = first_match(text, MEASUREMENTS)
        location, l_alias = first_match(text, LOCATIONS)
        condition, c_alias = first_match(text, CONDITIONS)
        event_type, e_alias = first_match(text, EVENTS)
        time_expression, t_alias = first_match(text, TIMES)

        threshold_type = ""
        if intent == "threshold":
            if contains(text, "high"):
                threshold_type = "high"
            elif contains(text, "low"):
                threshold_type = "low"

        if not time_expression:
            time_expression = "latest" if intent == "current_data" else ""

        removable = set(STOPWORDS)
        for phrase in (m_alias, l_alias, c_alias, e_alias, t_alias):
            removable.update(normalize(phrase).split())
        equipment_terms = tuple(
            token for token in text.split()
            if token not in removable and len(token) > 1 and not token.isdigit()
        )

        return Concepts(
            question=question,
            normalized=text,
            intent=intent,
            equipment_terms=equipment_terms,
            measurement=measurement,
            location=location,
            condition=condition,
            event_type=event_type,
            threshold_type=threshold_type,
            time_expression=time_expression,
        )
