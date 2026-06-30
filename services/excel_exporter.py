import io
from datetime import datetime
from typing import List, Dict
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── Colour palette ─────────────────────────────────────────────────────────────
BLUE_DARK    = "0C447C"
BLUE_MID     = "185FA5"
BLUE_LIGHT   = "E6F1FB"
GREEN_DARK   = "27500A"
GREEN_MID    = "3B6D11"
GREEN_LIGHT  = "EAF3DE"
PURPLE_DARK  = "3C3489"
PURPLE_MID   = "534AB7"
PURPLE_LIGHT = "EEEDFE"
WHITE        = "FFFFFF"
GRAY_LIGHT   = "F1EFE8"
GRAY_DARK    = "2C2C2A"
AMBER        = "FAEEDA"
AMBER_DARK   = "854F0B"

COPYRIGHT         = "© 2026 News Digest Platform. Internal use only. All rights reserved."
ARTICLE_ROW_HEIGHT = 65


# ── Style helpers ──────────────────────────────────────────────────────────────
def _fill(hex_color: str | None) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color or GRAY_DARK)

def _font(bold=False, size=10, color=GRAY_DARK, underline=None) -> Font:
    return Font(name="Calibri", bold=bold, size=size,
                color=color, underline=underline)

def _border() -> Border:
    s = Side(style="thin", color="D3D1C7")
    return Border(left=s, right=s, top=s, bottom=s)

def _align(h="left", v="top", wrap=True) -> Alignment:
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

def _center() -> Alignment:
    return Alignment(horizontal="center", vertical="center", wrap_text=True)


# ── Shared row writers ─────────────────────────────────────────────────────────
def _title_row(ws, text: str, ncols: int, bg: str, row=1):
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=text)
    c.font      = _font(bold=True, size=14, color=WHITE)
    c.fill      = _fill(bg) if bg else _fill(GRAY_DARK)
    c.alignment = _center()
    ws.row_dimensions[row].height = 28

def _period_row(ws, window: dict | None, ncols: int, bg: str = "", row: int = 2):
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=ncols)
    label = (window or {}).get("label", "")
    c = ws.cell(row=row, column=1,
                value=f"Period: {label}")
    c.font      = _font(bold=False, size=11, color=WHITE)
    c.fill      = _fill(bg)
    c.alignment = _center()
    ws.row_dimensions[row].height = 18

def _header_row(ws, headers: list, row: int, bg: str):
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=ci, value=h)
        c.font      = _font(bold=True, size=10, color=WHITE)
        c.fill      = _fill(bg)
        c.alignment = _center()
        c.border    = _border()
    ws.row_dimensions[row].height = 22

def _data_cell(ws, row: int, col: int, value, fill: PatternFill,
               hyperlink: str | None = None):
    c = ws.cell(row=row, column=col, value=value)
    c.fill      = fill
    c.border    = _border()
    if hyperlink:
        c.hyperlink = hyperlink
        c.font      = _font(size=10, color=BLUE_MID, underline="single")
        c.alignment = _align()
    else:
        c.font      = _font(size=10)
        c.alignment = _align()
    return c

def _set_widths(ws, widths: list):
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w

def _copyright_row(ws, ncols: int, row: int):
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=COPYRIGHT)
    c.font      = _font(size=9, color="888880")
    c.alignment = _center()
    ws.row_dimensions[row].height = 14

def _parse_date(date_str: str):
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%B"), dt.strftime("%B %Y"), dt.strftime("%d %b %Y")
    except Exception:
        return "", "", date_str


# ── Summary sheet ──────────────────────────────────────────────────────────────
def _build_summary(wb, client_data, prospect_data,
                   industry_data, window: dict | None):
    ws = wb.create_sheet("Summary", 0)
    _set_widths(ws, [32, 20])

    _title_row(ws, "News Digest Platform — Export Summary", 2, GRAY_DARK, row=1)

    ws.merge_cells("A2:B2")
    c = ws.cell(row=2, column=1,
                value=f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}")
    c.font = _font(size=10, color="5F5E5A")
    c.alignment = _center()

    ws.merge_cells("A3:B3")
    label = window.get("label", "") if window else ""
    c = ws.cell(row=3, column=1, value=f"Period: {label}")
    c.font = _font(bold=True, size=10, color=GRAY_DARK)
    c.alignment = _center()
    ws.row_dimensions[3].height = 16

    rows = [
        ("Category",    "Articles",  True),
        ("Clients",
         sum(len(i["news"]) for i in client_data),   False),
        ("Prospects",
         sum(len(i["news"]) for i in prospect_data), False),
        ("Industries",
         sum(len(i["news"]) for i in industry_data), False),
        ("",            "",          False),
        ("Total articles exported",
         sum(len(i["news"])
             for i in client_data + prospect_data + industry_data), True),
    ]
    for ri, (label, value, bold) in enumerate(rows, 4):
        ca = ws.cell(row=ri, column=1, value=label)
        cb = ws.cell(row=ri, column=2, value=value)
        fill = _fill(GRAY_LIGHT) if bold else _fill(WHITE)
        for c in (ca, cb):
            c.font      = _font(bold=bold, size=10)
            c.fill      = fill
            c.alignment = _align("left" if c == ca else "center")
            if label:
                c.border = _border()
        ws.row_dimensions[ri].height = 18

    _copyright_row(ws, 2, len(rows) + 5)
    ws.freeze_panes = "A4"


# ── Sheet setup helper ─────────────────────────────────────────────────────────
def _sheet_header(ws, title: str, headers: list, window, dark_color: str, mid_color: str) -> int:
    """Write title / period / column-header rows and return the column count."""
    ncols = len(headers)
    _title_row(ws,  title,   ncols, dark_color)
    _period_row(ws, window,  ncols, mid_color)
    _header_row(ws, headers, row=3, bg=mid_color)
    return ncols


# ── Client Report sheet ────────────────────────────────────────────────────────
def _build_client_sheet(wb, client_data: List[Dict], window: dict | None):
    """
    Columns: Company Name | News Article Header | News Article Details
             (with Insights) | Source | Date | Month | Hyperlink | Client/Prospect
    """
    ws      = wb.create_sheet("Client Report")
    headers = [
        "Company Name", "News Article Header",
        "News Article Details (with Insights)",
        "Source", "Date", "Month", "Hyperlink", "Client/Prospect",
    ]
    ncols = _sheet_header(ws, "Client News Report", headers, window, BLUE_DARK, BLUE_MID)
    _set_widths(ws, [24, 36, 55, 20, 14, 14, 14, 16])

    row = 4
    for item in client_data:
        entity = item["entity"]
        for idx, n in enumerate(item["news"]):
            month, _, date_fmt = _parse_date(n.published_date)
            fill = _fill(BLUE_LIGHT) if idx % 2 == 0 else _fill(WHITE)
            insight = (n.summary or n.title)

            _data_cell(ws, row, 1, entity.name,      fill)
            _data_cell(ws, row, 2, n.title,           fill)   # original title
            _data_cell(ws, row, 3, insight,           fill)
            _data_cell(ws, row, 4, n.source,          fill)
            _data_cell(ws, row, 5, date_fmt,          fill)
            _data_cell(ws, row, 6, month,             fill)
            link_label = "⚠ View Article" if n.paywall_note else "View Article"
            _data_cell(ws, row, 7, link_label,        fill,
                       hyperlink=n.url or n.original_url)
            _data_cell(ws, row, 8, "Client",          fill)

            ws.row_dimensions[row].height = ARTICLE_ROW_HEIGHT
            row += 1

    _copyright_row(ws, ncols, row + 1)
    ws.freeze_panes = "A4"


# ── Prospect Report sheet ──────────────────────────────────────────────────────
def _build_prospect_sheet(wb, prospect_data: List[Dict], window: dict | None):
    """
    Columns: Company | News Article Header | News Article Details |
             Source | Month, Year | Date | News HyperLink | Client/Prospect
    """
    ws      = wb.create_sheet("Prospect Report")
    headers = [
        "Company", "News Article Header", "News Article Details",
        "Source", "Month, Year", "Date", "News HyperLink", "Client/Prospect",
    ]
    ncols = _sheet_header(ws, "Prospect News Report", headers, window, GREEN_DARK, GREEN_MID)
    _set_widths(ws, [24, 36, 55, 20, 16, 14, 14, 16])

    row = 4
    for item in prospect_data:
        entity = item["entity"]
        for idx, n in enumerate(item["news"]):
            month, month_year, date_fmt = _parse_date(n.published_date)
            fill = _fill(GREEN_LIGHT) if idx % 2 == 0 else _fill(WHITE)

            _data_cell(ws, row, 1, entity.name,    fill)
            _data_cell(ws, row, 2, n.title,         fill)   # original title
            _data_cell(ws, row, 3, n.summary or n.title, fill)
            _data_cell(ws, row, 4, n.source,        fill)
            _data_cell(ws, row, 5, month_year,      fill)
            _data_cell(ws, row, 6, date_fmt,        fill)
            _data_cell(ws, row, 7, "View Article",  fill,
                       hyperlink=n.url or n.original_url)
            _data_cell(ws, row, 8, "Prospect",      fill)

            ws.row_dimensions[row].height = ARTICLE_ROW_HEIGHT
            row += 1

    _copyright_row(ws, ncols, row + 1)
    ws.freeze_panes = "A4"


# ── Industry News sheet ────────────────────────────────────────────────────────
def _build_industry_sheet(wb, industry_data: List[Dict], window: dict | None):
    """
    Columns: Industry | Category | News Article Header |
             News Article Details (with Insights) |
             Source | Month | Date | Hyperlink
    """
    ws      = wb.create_sheet("Industry News")
    headers = [
        "Industry", "Category",
        "News Article Header",
        "News Article Details (with Insights)",
        "Source", "Month", "Date", "Hyperlink",
    ]
    ncols = _sheet_header(ws, "Industry News Report", headers, window, PURPLE_DARK, PURPLE_MID)
    _set_widths(ws, [22, 32, 36, 55, 20, 14, 14, 14])

    row = 4
    for item in industry_data:
        entity   = item["entity"]
        for idx, n in enumerate(item["news"]):
            month, _, date_fmt = _parse_date(n.published_date)
            fill     = _fill(PURPLE_LIGHT) if idx % 2 == 0 else _fill(WHITE)
            category = n.primary_category or ", ".join(entity.topics) or "General"

            _data_cell(ws, row, 1, entity.name,          fill)
            _data_cell(ws, row, 2, category,              fill)
            _data_cell(ws, row, 3, n.title,               fill)  # original title
            _data_cell(ws, row, 4, n.summary or n.title,  fill)
            _data_cell(ws, row, 5, n.source,              fill)
            _data_cell(ws, row, 6, month,                 fill)
            _data_cell(ws, row, 7, date_fmt,              fill)
            _data_cell(ws, row, 8, "View Article",        fill,
                       hyperlink=n.url or n.original_url)

            ws.row_dimensions[row].height = ARTICLE_ROW_HEIGHT
            row += 1

    _copyright_row(ws, ncols, row + 1)
    ws.freeze_panes = "A4"


# ── Gap Report sheet ───────────────────────────────────────────────────────────
def _build_gap_sheet(wb, gap_report: dict):
    """
    Lists all entities with no news at all, and per-entity topic gaps.
    The gap_report dict already contains the window from build_gap_report().
    """
    if not gap_report:
        return

    ws    = wb.create_sheet("Gap Report")
    ncols = 3
    _title_row(ws, "News Gap Report", ncols, AMBER_DARK)
    _period_row(ws, gap_report.get("window"), ncols, AMBER_DARK, row=2)
    _set_widths(ws, [28, 20, 45])

    # Section 1 — no news at all (shifted down one row for period row)
    ws.merge_cells(f"A4:C4")
    c = ws.cell(row=4, column=1, value="Entities with NO news in this period")
    c.font = _font(bold=True, size=10, color=AMBER_DARK)
    c.fill = _fill(AMBER)
    c.alignment = _align()

    _header_row(ws, ["Entity Name", "Type", "Reason"], row=5, bg=AMBER_DARK)

    row = 6
    for item in gap_report.get("no_news_at_all", []):
        fill = _fill(AMBER) if row % 2 == 0 else _fill(WHITE)
        _data_cell(ws, row, 1, item["name"], fill)
        _data_cell(ws, row, 2, item["type"], fill)
        _data_cell(ws, row, 3, "No articles found across all 12 topics", fill)
        ws.row_dimensions[row].height = 18
        row += 1

    # Section 2 — topic gaps
    row += 1
    ws.merge_cells(f"A{row}:C{row}")
    c = ws.cell(row=row, column=1, value="Entities with topic-level gaps")
    c.font = _font(bold=True, size=10, color=AMBER_DARK)
    c.fill = _fill(AMBER)
    c.alignment = _align()
    row += 1

    _header_row(ws, ["Entity Name", "Type", "Topics with no articles"], row=row, bg=AMBER_DARK)
    row += 1

    for name, info in gap_report.get("topic_gaps", {}).items():
        fill = _fill(AMBER) if row % 2 == 0 else _fill(WHITE)
        missing = ", ".join(info["missing_topics"])
        _data_cell(ws, row, 1, name,               fill)
        _data_cell(ws, row, 2, info["entity_type"], fill)
        _data_cell(ws, row, 3, missing,             fill)
        ws.row_dimensions[row].height = 40
        row += 1

    _copyright_row(ws, ncols, row + 1)
    ws.freeze_panes = "A6"


# ── Full analytics sheet ───────────────────────────────────────────────────────
def _build_full_analytics(wb, entities_map: dict, all_news: dict,
                          audit_entries: list, window: dict | None = None):
    """
    Single sheet with every article (accepted + rejected) including reason.
    Columns: Entity | Type | Status | Article Title | Source | URL |
             Published | Period | Primary Category | Secondary Category |
             Topic | Fetch Source | Reason / Note
    """
    RED_LIGHT    = "FEE2E2"
    RED_MID      = "B91C1C"
    ORANGE_LIGHT = "FEF9C3"
    ORANGE_MID   = "B45309"
    GREEN_LIGHT2 = "DCFCE7"
    GREEN_MID2   = "166534"

    ws      = wb.create_sheet("Full Analytics")
    headers = [
        "Entity Name", "Entity Type", "Status", "Reason / Note",
        "Article Title", "Source", "URL",
        "Published Date", "Period",
        "Primary Category", "Secondary Category",
        "Topic Queried", "Fetch Source",
    ]
    ncols        = len(headers)
    period_label = (window or {}).get("label", "All time")
    _title_row(ws, "Full Analytics — All Articles (Accepted + Rejected)", ncols, GRAY_DARK)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    c = ws.cell(row=2, column=1,
                value=f"Period: {period_label}  |  "
                      f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}  |  "
                      f"Accepted: {sum(len(v) for v in all_news.values())}  |  "
                      f"Audit entries: {len(audit_entries)}")
    c.font      = _font(size=10, color=WHITE)
    c.fill      = _fill(GRAY_DARK)
    c.alignment = _center()

    _header_row(ws, headers, row=3, bg=GRAY_DARK)
    _set_widths(ws, [22, 12, 14, 40, 44, 20, 14, 14, 28, 30, 28, 28, 14])

    row = 4

    # ── Accepted articles from news data ──────────────────────────────────────
    for eid, items in all_news.items():
        entity = entities_map.get(eid)
        entity_name = entity.name if entity else eid
        entity_type = entity.entity_type.value if entity else "unknown"
        for idx, n in enumerate(items):
            fill = _fill(GREEN_LIGHT2) if idx % 2 == 0 else _fill(WHITE)
            _data_cell(ws, row, 1,  entity_name,             fill)
            _data_cell(ws, row, 2,  entity_type.capitalize(), fill)
            c = ws.cell(row=row, column=3, value="Accepted")
            c.font      = _font(bold=True, size=10, color=GREEN_MID2)
            c.fill      = fill
            c.border    = _border()
            c.alignment = _align()
            _data_cell(ws, row, 4,  "Passed all checks",     fill)
            _data_cell(ws, row, 5,  n.title,                 fill)
            _data_cell(ws, row, 6,  n.source,                fill)
            _data_cell(ws, row, 7,  "View",                  fill, hyperlink=n.url or n.original_url)
            _data_cell(ws, row, 8,  n.published_date,        fill)
            _data_cell(ws, row, 9,  n.period,                fill)
            _data_cell(ws, row, 10, n.primary_category,      fill)
            _data_cell(ws, row, 11, n.secondary_category or "", fill)
            _data_cell(ws, row, 12, n.topic_queried or "",   fill)
            _data_cell(ws, row, 13, n.fetch_source or "",    fill)
            ws.row_dimensions[row].height = 40
            row += 1

    # ── Rejected / duplicate entries from audit log ───────────────────────────
    non_accepted = [e for e in audit_entries if e.action != "accepted"]
    for idx, entry in enumerate(non_accepted):
        action = entry.action  # duplicate_removed | validation_rejected | url_invalid
        if action == "duplicate_removed":
            fill     = _fill(ORANGE_LIGHT) if idx % 2 == 0 else _fill(WHITE)
            status   = "Duplicate"
            s_color  = ORANGE_MID
        else:
            fill     = _fill(RED_LIGHT) if idx % 2 == 0 else _fill(WHITE)
            status   = action.replace("_", " ").title()
            s_color  = RED_MID

        entity = entities_map.get(entry.entity_id)
        entity_type = entity.entity_type.value.capitalize() if entity else "Unknown"

        _data_cell(ws, row, 1,  entry.entity_name,  fill)
        _data_cell(ws, row, 2,  entity_type,         fill)
        c = ws.cell(row=row, column=3, value=status)
        c.font      = _font(bold=True, size=10, color=s_color)
        c.fill      = fill
        c.border    = _border()
        c.alignment = _align()
        _data_cell(ws, row, 4,  entry.reason,        fill)
        _data_cell(ws, row, 5,  entry.article_title, fill)
        _data_cell(ws, row, 6,  "",                  fill)
        _data_cell(ws, row, 7,  "View" if entry.source_url else "", fill,
                   hyperlink=entry.source_url or None)
        _data_cell(ws, row, 8,  entry.run_date,      fill)
        _data_cell(ws, row, 9,  "",                  fill)
        _data_cell(ws, row, 10, "",                  fill)
        _data_cell(ws, row, 11, "",                  fill)
        _data_cell(ws, row, 12, "",                  fill)
        _data_cell(ws, row, 13, "",                  fill)
        ws.row_dimensions[row].height = 40
        row += 1

    _copyright_row(ws, ncols, row + 1)
    ws.freeze_panes = "A4"


# ── Public entry point (standard digest) ──────────────────────────────────────
def generate_excel_report(
    client_data:   List[Dict],
    prospect_data: List[Dict],
    industry_data: List[Dict],
    window:        dict | None = None,
    gap_report:    dict | None = None,
) -> bytes:
    """
    Generates a multi-sheet .xlsx report and returns bytes for streaming.

    Sheets:
        1. Summary
        2. Client Report
        3. Prospect Report
        4. Industry News
        5. Gap Report (if gap_report provided)
    """
    wb = openpyxl.Workbook()
    active = wb.active
    if active is not None:
        wb.remove(active)

    _build_summary(wb, client_data, prospect_data, industry_data, window)
    _build_client_sheet(wb,   client_data,   window)
    _build_prospect_sheet(wb, prospect_data, window)
    _build_industry_sheet(wb, industry_data, window)

    if gap_report:
        _build_gap_sheet(wb, gap_report)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def generate_full_analytics_report(
    entities:      list,
    all_news:      dict,
    audit_entries: list,
    window:        dict | None = None,
) -> bytes:
    """
    Generates a single-sheet .xlsx with every article (accepted + rejected).
    entities: List[Entity]
    all_news: Dict[entity_id, List[NewsItem]]
    audit_entries: List[AuditEntry]
    window: date range dict with 'label', 'from', 'to' keys
    """
    entities_map = {e.id: e for e in entities}

    wb = openpyxl.Workbook()
    active = wb.active
    if active is not None:
        wb.remove(active)

    _build_full_analytics(wb, entities_map, all_news, audit_entries, window)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()