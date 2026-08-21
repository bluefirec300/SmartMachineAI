"""
Shared PDF-export helper for pages that offer a "Download PDF" button
(Event Records, Energy Dashboard - Phase V2.6).

Deliberately tiny and generic, mirroring ui/csv_export.py's own scope:
takes a title, a small "report parameters" header block (e.g. active
filters, plant, date range - whatever the page already resolved), and
a plain DataFrame it already built for the CSV export - never computes
or fetches anything itself. No report designer, no charts, no
page-layout configuration - one title, one parameter block, one table,
same as reportlab's own SimpleDocTemplate defaults.
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_STYLES = getSampleStyleSheet()

_TITLE_STYLE = ParagraphStyle("ReportTitle", parent=_STYLES["Title"], fontSize=16, spaceAfter=4)
_PARAM_STYLE = ParagraphStyle("ReportParam", parent=_STYLES["Normal"], fontSize=9, textColor=colors.HexColor("#444444"))
_CELL_STYLE = ParagraphStyle("Cell", parent=_STYLES["Normal"], fontSize=8, leading=10)
_HEADER_CELL_STYLE = ParagraphStyle("HeaderCell", parent=_CELL_STYLE, textColor=colors.white, fontName="Helvetica-Bold")

# Same row-highlight convention as ui/table_style.py's on-screen palette
# (Event Records/Anomalies) - a color means the same thing on screen and
# on paper.
_ROW_HIGHLIGHT_COLORS = {
    "ALARM": colors.HexColor("#ffb3b3"),
    "WARNING": colors.HexColor("#ffe6a3"),
}


def export_filename(prefix: str, extension: str = "pdf") -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}.{extension}"


def build_pdf_report(
    title: str,
    parameters: dict[str, str],
    df: pd.DataFrame,
    *,
    highlight_column: str | None = None,
    generated_at: str | None = None,
) -> bytes:
    """
    Renders `df` exactly as given (same values as whatever the page's
    CSV export already uses - no recomputation) into a simple one-title,
    one-parameter-block, one-table PDF. `highlight_column`, if given,
    names a column whose value (e.g. "ALARM"/"WARNING") is looked up in
    _ROW_HIGHLIGHT_COLORS to shade that row - purely cosmetic, mirrors
    the on-screen severity coloring, never changes what data is shown.
    """
    buffer = io.BytesIO()
    page_size = landscape(A4) if len(df.columns) > 5 else A4
    doc = SimpleDocTemplate(
        buffer, pagesize=page_size,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
    )

    story = [Paragraph(title, _TITLE_STYLE)]

    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    story.append(Paragraph(f"Generated {generated_at}", _PARAM_STYLE))

    for label, value in parameters.items():
        story.append(Paragraph(f"<b>{label}:</b> {value}", _PARAM_STYLE))

    story.append(Spacer(1, 8 * mm))

    if df.empty:
        story.append(Paragraph("No matching data.", _STYLES["Normal"]))
    else:
        header_row = [Paragraph(str(col), _HEADER_CELL_STYLE) for col in df.columns]
        data_rows = [
            [Paragraph(_escape(value), _CELL_STYLE) for value in row]
            for row in df.astype(str).itertuples(index=False)
        ]
        table = Table([header_row] + data_rows, repeatRows=1)

        style_commands = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c4a63")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ]

        if highlight_column is not None and highlight_column in df.columns:
            column_index = list(df.columns).index(highlight_column)
            for row_index, value in enumerate(df[highlight_column], start=1):
                color = _ROW_HIGHLIGHT_COLORS.get(str(value))
                if color is not None:
                    style_commands.append(("BACKGROUND", (0, row_index), (-1, row_index), color))

        table.setStyle(TableStyle(style_commands))
        story.append(table)

    doc.build(story)
    return buffer.getvalue()


def _escape(value: str) -> str:
    """Reportlab's Paragraph interprets a small XML-like markup subset
    (used deliberately above for <b>) - plain cell values must have
    their own literal &/</> escaped first so they render as text, not
    (silently broken) markup."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
