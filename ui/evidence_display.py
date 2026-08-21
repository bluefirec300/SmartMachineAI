from __future__ import annotations

from typing import Any

import streamlit as st

"""
UI polish phase - readable rendering for the Evidence/Assumptions/Data
limitations JSON blobs Anomalies and Energy Opportunities both store
(engine.anomaly_engine, engine.opportunity_engine). Replaces a raw
`st.json(json.loads(raw))` dump - technically correct, but it exposes
internal field names (e.g. "context_used", "fault_window_noted") and
JSON punctuation directly to the engineer, which read as "internal
programmer" output rather than an engineering explanation. Renders the
SAME data, just as plain labeled text/bullets - never drops, reorders,
or reinterprets a value, and falls back to raw JSON for any shape this
doesn't recognize rather than guessing.
"""


def _label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def render_readable(data: Any) -> None:
    if data in (None, [], {}, ""):
        st.write("None recorded.")
        return

    if isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                st.json(item)  # a shape this renderer doesn't know how to flatten - show it honestly, not guessed
            else:
                st.markdown(f"- {item}")
        return

    if isinstance(data, dict):
        for key, value in data.items():
            label = _label(str(key))

            if isinstance(value, list) and value:
                st.markdown(f"**{label}:**")
                for item in value:
                    st.markdown(f"- {item}" if not isinstance(item, (dict, list)) else str(item))
            elif isinstance(value, dict) and value:
                st.markdown(f"**{label}:** " + ", ".join(f"{_label(str(k))}: {v}" for k, v in value.items()))
            elif isinstance(value, bool):
                st.markdown(f"**{label}:** {'Yes' if value else 'No'}")
            elif value not in (None, [], {}, ""):
                st.markdown(f"**{label}:** {value}")
        return

    st.write(data)
