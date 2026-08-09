from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


st.set_page_config(
    page_title="SmartMachineAI",
    page_icon="🏭",
    layout="wide",
)

st.title("🏭 SmartMachineAI")

st.markdown(
    """
Use the pages in the sidebar:

- **Live Data** - current value of every enabled tag, grouped by
  equipment, with engineering-limit status.
- **Setpoints** - view/edit the low/high warning and alarm limits.
- **Service & Maintenance** - two tabs: Service (one-off service
  visits, no schedule) and Maintenance (upcoming/overdue tracking and
  a log of past work).
- **Ask AI** - ask questions in plain English ("why is the compressor
  pressure dropping") without needing the terminal.
- **Event Records** - browse the full alarm/warning history, filterable
  by severity, equipment, tag, and time range.

This shows whatever is currently **enabled** in `database/config.db`
- as more of the P01/P02/Phase 2/3 tag dataset gets enabled
(`engine/tag_dataset_importer.py`), it appears here automatically,
with no changes needed to this app.
"""
)
