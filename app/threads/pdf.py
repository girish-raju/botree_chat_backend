"""Render a conversation thread as a branded PDF report.

`render_thread_pdf` turns the persisted (opaque `aui/v6`) messages of a
thread into an insights-only report: Botree masthead, generation date (IST),
the user's data-access scope ("applied filters"), then one section per Q&A
with the question, the answer prose, and the full result table rebuilt from
the `query_database` tool output. SQL, follow-up suggestions and abandoned
edit-branches are excluded.

Everything is defensive: message content the renderer can't parse is skipped,
and a missing font/logo degrades the styling rather than failing the export.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from fpdf import FPDF
from fpdf.fonts import FontFace

from app.db.models import Message, User
from app.domain.formatting import format_rupees, is_money_column
from app.rbac.profiles import profile_from_user

logger = structlog.get_logger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_LOGO_PATH = _ASSETS_DIR / "botree-logo.png"
_FONT_REGULAR = _ASSETS_DIR / "fonts" / "DejaVuSans.ttf"
_FONT_BOLD = _ASSETS_DIR / "fonts" / "DejaVuSans-Bold.ttf"

_IST = ZoneInfo("Asia/Kolkata")

#: Human labels for the RBAC geo columns, for the "applied filters" line.
_GEO_LABELS = {
    "geo_hier2_name": "Zone",
    "geo_hier3_name": "Region",
    "geo_hier4_name": "State",
    "geo_hier6_name": "District",
    "geo_hier7_name": "Town",
}

#: Keep pathological cell values from blowing up the layout.
_MAX_CELL_CHARS = 300

_BRAND_RGB = (114, 9, 183)  # Botree brand purple, #7209B7
_MUTED_RGB = (110, 110, 118)
_RULE_RGB = (225, 225, 230)
_HEAD_FILL_RGB = (243, 243, 246)


# ---------------------------------------------------------------------------
# Content extraction (pure helpers — unit-testable without fpdf)
# ---------------------------------------------------------------------------


def active_branch(head_id: str | None, messages: list[Message]) -> list[Message]:
    """Return only the active branch, oldest-first.

    `list_messages` returns every message ever stored, including branches
    abandoned by message edits. The current conversation is the `parent_id`
    chain ending at the thread's `head_id`. Falls back to created_at order
    when there is no usable head (old threads, missing rows).
    """
    if not head_id:
        return list(messages)
    by_id = {m.id: m for m in messages}
    if head_id not in by_id:
        return list(messages)
    chain: list[Message] = []
    seen: set[str] = set()
    cursor: str | None = head_id
    while cursor is not None and cursor in by_id and cursor not in seen:
        seen.add(cursor)
        message = by_id[cursor]
        chain.append(message)
        cursor = message.parent_id
    chain.reverse()
    return chain


def split_prose(text: str) -> str:
    """Answer prose without the deterministic markdown table.

    The pipeline appends the full result table to the answer text as
    ``"\\n\\n" + "| col | ... |"`` — everything from the first table line on
    is dropped (the PDF rebuilds the table from the structured tool output).
    """
    prose_lines: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            break
        prose_lines.append(line)
    return "\n".join(prose_lines).strip()


def _text_of(parts: list[Any]) -> str:
    chunks = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
    ]
    return "\n".join(chunks).strip()


def _tables_of(parts: list[Any]) -> list[dict[str, Any]]:
    """Extract `{columns, rows, row_count}` from `tool-*` parts, if any."""
    tables: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if not isinstance(part_type, str) or not part_type.startswith("tool-"):
            continue
        output = part.get("output")
        if not isinstance(output, dict):
            continue
        columns = output.get("columns")
        rows = output.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            continue
        if not columns or not rows:
            continue
        if len(rows) == 1 and len(columns) == 1:
            continue  # scalar answers are already stated in the prose
        tables.append(
            {
                "columns": [str(c) for c in columns],
                "rows": [r for r in rows if isinstance(r, dict)],
                "row_count": output.get("row_count", len(rows)),
            }
        )
    return tables


def extract_sections(messages: list[Message]) -> list[dict[str, Any]]:
    """Flatten the branch into renderable blocks, skipping unparseable ones.

    Returns dicts of either ``{"kind": "question", "text"}`` or
    ``{"kind": "answer", "prose", "tables"}``.
    """
    sections: list[dict[str, Any]] = []
    for message in messages:
        try:
            content = message.content
            if not isinstance(content, dict):
                continue
            role = content.get("role")
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            if role == "user":
                text = _text_of(parts)
                if text:
                    sections.append({"kind": "question", "text": text})
            elif role == "assistant":
                sections.append(
                    {
                        "kind": "answer",
                        "prose": split_prose(_text_of(parts)),
                        "tables": _tables_of(parts),
                    }
                )
        except Exception:  # malformed content must never break the export
            logger.warning("pdf_message_skipped", message_id=message.id, exc_info=True)
    return sections


def scope_line(user: User) -> str:
    """The 'applied filters' line: the RBAC scope every query ran under."""
    profile = profile_from_user(user)
    if profile.is_unrestricted:
        return f"Role {user.role} · Full access"
    if profile.geo_vals:
        label = _GEO_LABELS.get(profile.geo_col or "", "Scope")
        return f"Role {user.role} · {label}: {', '.join(profile.geo_vals)}"
    return f"Role {user.role}"


def _cell_text(column: str, value: Any) -> str:
    if value is None:
        return ""
    if (
        is_money_column(column)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return format_rupees(value)
    text = str(value)
    return text if len(text) <= _MAX_CELL_CHARS else text[: _MAX_CELL_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# PDF assembly
# ---------------------------------------------------------------------------


class _ReportPDF(FPDF):
    """A4 report with a page-number footer; fonts resolved at init."""

    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_margins(left=15, top=14, right=15)
        self.set_auto_page_break(auto=True, margin=18)
        self.brand_font = self._register_fonts()

    def _register_fonts(self) -> str:
        """Register DejaVu (full Unicode incl. ₹); fall back to helvetica."""
        try:
            self.add_font("DejaVu", "", str(_FONT_REGULAR))
            self.add_font("DejaVu", "B", str(_FONT_BOLD))
            return "DejaVu"
        except Exception:
            logger.warning("pdf_font_load_failed", exc_info=True)
            return "helvetica"

    def txt(self, text: str) -> str:
        """Make `text` safe for the active font (latin-1 core fonts only)."""
        if self.brand_font != "helvetica":
            return text
        return (
            text.replace("₹", "Rs. ")
            .encode("latin-1", "replace")
            .decode("latin-1")
        )

    def footer(self) -> None:  # called by fpdf on every page
        self.set_y(-14)
        self.set_font(self.brand_font, size=7)
        self.set_text_color(*_MUTED_RGB)
        self.cell(
            0, 5, self.txt(f"Generated by Botree AI · Page {self.page_no()}/{{nb}}"),
            align="C",
        )


def _masthead(pdf: _ReportPDF, user: User, generated_at: datetime) -> None:
    """Header band: logo + wordmark on the LEFT, meta lines on the RIGHT."""
    top = pdf.get_y()
    text_x = pdf.l_margin
    if _LOGO_PATH.exists():
        try:
            pdf.image(str(_LOGO_PATH), x=pdf.l_margin, y=top, w=13, h=13)
            text_x = pdf.l_margin + 17
        except Exception:
            logger.warning("pdf_logo_embed_failed", exc_info=True)

    pdf.set_xy(text_x, top)
    pdf.set_font(pdf.brand_font, style="B", size=17)
    pdf.set_text_color(*_BRAND_RGB)
    pdf.cell(0, 8, pdf.txt("Botree AI"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(text_x)
    pdf.set_font(pdf.brand_font, size=10)
    pdf.set_text_color(*_MUTED_RGB)
    pdf.cell(0, 5, pdf.txt("Conversation Report"), new_x="LMARGIN", new_y="NEXT")

    # Right-aligned meta block, vertically level with the wordmark.
    pdf.set_font(pdf.brand_font, size=9)
    pdf.set_text_color(*_MUTED_RGB)
    meta = [
        f"Generated: {generated_at.strftime('%d %B %Y, %I:%M %p')} IST",
        f"Generated for: {user.display_name}",
        f"Applied filters: {scope_line(user)}",
    ]
    for index, line in enumerate(meta):
        pdf.set_xy(pdf.l_margin, top + index * 5)
        pdf.cell(0, 5, pdf.txt(line), align="R")

    pdf.set_y(max(top + 15, top + len(meta) * 5) + 4)
    pdf.set_draw_color(*_RULE_RGB)
    pdf.set_line_width(0.4)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)


def _render_table(pdf: _ReportPDF, table_data: dict[str, Any]) -> None:
    columns: list[str] = table_data["columns"]
    rows: list[dict[str, Any]] = table_data["rows"]
    row_count: int = table_data["row_count"]

    # Wide tables get a smaller face; cells wrap and pages break automatically.
    font_size = 8 if len(columns) <= 6 else 7
    pdf.set_font(pdf.brand_font, size=font_size)
    pdf.set_text_color(25)
    pdf.set_draw_color(*_RULE_RGB)
    pdf.set_line_width(0.2)

    headings = FontFace(emphasis="BOLD", fill_color=_HEAD_FILL_RGB)
    with pdf.table(
        headings_style=headings,
        line_height=1.6 * font_size / 2.2,
        padding=1.2,
        text_align="LEFT",
    ) as table:
        header = table.row()
        for column in columns:
            header.cell(pdf.txt(column))
        for row in rows:
            body = table.row()
            for column in columns:
                body.cell(pdf.txt(_cell_text(column, row.get(column))))

    if row_count > len(rows):
        pdf.ln(1)
        pdf.set_font(pdf.brand_font, size=7.5)
        pdf.set_text_color(*_MUTED_RGB)
        pdf.cell(
            0, 4, pdf.txt(f"Showing first {len(rows)} of {row_count} rows."),
            new_x="LMARGIN", new_y="NEXT",
        )


def render_thread_pdf(
    *,
    head_id: str | None,
    messages: list[Message],
    user: User,
    generated_at: datetime | None = None,
) -> bytes:
    """Build the full report PDF and return its bytes."""
    generated_at = generated_at or datetime.now(_IST)
    sections = extract_sections(active_branch(head_id, messages))

    pdf = _ReportPDF()
    pdf.add_page()
    _masthead(pdf, user, generated_at)

    if not sections:
        pdf.set_font(pdf.brand_font, size=10)
        pdf.set_text_color(*_MUTED_RGB)
        pdf.cell(0, 6, pdf.txt("No messages in this conversation."))
        return bytes(pdf.output())

    question_no = 0
    for section in sections:
        if section["kind"] == "question":
            question_no += 1
            # Keep the question heading attached to at least a little content.
            if pdf.will_page_break(24):
                pdf.add_page()
            pdf.ln(3)
            pdf.set_font(pdf.brand_font, style="B", size=11)
            pdf.set_text_color(*_BRAND_RGB)
            pdf.multi_cell(
                0, 6, pdf.txt(f"Q{question_no}. {section['text']}"),
                new_x="LMARGIN", new_y="NEXT",
            )
            pdf.ln(0.5)
        else:
            if section["prose"]:
                pdf.set_font(pdf.brand_font, size=10)
                pdf.set_text_color(35)
                pdf.multi_cell(
                    0, 5.4, pdf.txt(section["prose"]), new_x="LMARGIN", new_y="NEXT"
                )
                pdf.ln(1.5)
            for table_data in section["tables"]:
                _render_table(pdf, table_data)
                pdf.ln(2)

    return bytes(pdf.output())


def report_filename(title: str | None, generated_at: datetime | None = None) -> str:
    """ASCII-safe attachment filename: botree-report-<slug>-<date>.pdf."""
    generated_at = generated_at or datetime.now(_IST)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-").lower()[:40]
    slug = slug or "conversation"
    return f"botree-report-{slug}-{generated_at.strftime('%Y-%m-%d')}.pdf"


__all__ = [
    "render_thread_pdf",
    "report_filename",
    "active_branch",
    "split_prose",
    "extract_sections",
    "scope_line",
]
