"""Pure-Python PDF generator for printable Mukisa Foundation requisition forms."""

import textwrap
from django.utils import timezone


def _pdf_escape(text: str) -> str:
    """Escape a string for use inside a PDF literal string ``(...)``."""
    sanitized = (
        str(text or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )
    return (
        sanitized.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _wrap_lines(text: str, width: int) -> list[str]:
    """Wrap multi-line text to a character width while preserving paragraph breaks."""
    raw = str(text or "—").strip() or "—"
    lines: list[str] = []
    for paragraph in raw.splitlines():
        wrapped = textwrap.wrap(paragraph, width=width) if paragraph.strip() else [""]
        lines.extend(wrapped)
    return lines or ["—"]


class _PDFCanvas:
    """Minimal multi-page PDF 1.4 builder using standard Type1 Helvetica fonts."""

    PAGE_WIDTH = 595.28  # A4 width in points
    PAGE_HEIGHT = 841.89  # A4 height in points
    MARGIN_X = 42.0
    TOP_Y = 795.0
    BOTTOM_Y = 52.0

    def __init__(self):
        self.pages: list[list[str]] = [[]]
        self.y = self.TOP_Y

    @property
    def commands(self) -> list[str]:
        return self.pages[-1]

    def new_page(self):
        self.pages.append([])
        self.y = self.TOP_Y

    def ensure_space(self, height: float):
        if self.y - height < self.BOTTOM_Y:
            self.new_page()

    def text(
        self,
        x: float,
        y: float,
        value: str,
        font: str = "F1",
        size: float = 10.0,
    ):
        escaped = _pdf_escape(value)
        self.commands.append(
            f"BT /{font} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET"
        )

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        stroke: bool = True,
        fill_rgb: tuple[float, float, float] | None = None,
    ):
        ops = ["q"]
        if fill_rgb is not None:
            r, g, b = fill_rgb
            ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        ops.append("0.6 w")
        ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re")
        if fill_rgb is not None and stroke:
            ops.append("B")
        elif fill_rgb is not None:
            ops.append("f")
        else:
            ops.append("S")
        ops.append("Q")
        self.commands.append(" ".join(ops))

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.6):
        self.commands.append(
            f"q {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S Q"
        )

    def to_bytes(self) -> bytes:
        objects: list[bytes] = []

        def add_obj(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_regular_id = add_obj(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        )
        font_bold_id = add_obj(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
        )

        pages_id_placeholder = add_obj(b"")
        page_ids: list[int] = []

        for page_cmds in self.pages:
            stream_data = "\n".join(page_cmds).encode("latin-1")
            content_id = add_obj(
                f"<< /Length {len(stream_data)} >>\nstream\n".encode("latin-1")
                + stream_data
                + b"\nendstream"
            )
            page_dict = (
                f"<< /Type /Page /Parent {pages_id_placeholder} 0 R "
                f"/MediaBox [0 0 {self.PAGE_WIDTH:.2f} {self.PAGE_HEIGHT:.2f}] "
                f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("latin-1")
            page_ids.append(add_obj(page_dict))

        kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[pages_id_placeholder - 1] = (
            f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_ids)} >>"
        ).encode("latin-1")

        catalog_id = add_obj(
            f"<< /Type /Catalog /Pages {pages_id_placeholder} 0 R >>".encode("latin-1")
        )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for idx, obj_bytes in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{idx} 0 obj\n".encode("latin-1"))
            out.extend(obj_bytes)
            out.extend(b"\nendobj\n")

        xref_offset = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        out.extend(b"0000000000 65535 f \n")
        for offset in offsets:
            out.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))

        out.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF\n"
            ).encode("latin-1")
        )
        return bytes(out)


def build_requisition_pdf(requisition) -> bytes:
    """Render a requisition, its budget items, and approval history as a PDF."""
    pdf = _PDFCanvas()
    table_width = pdf.PAGE_WIDTH - 2 * pdf.MARGIN_X
    label_col_w = 135.0
    value_col_w = table_width - label_col_w
    left_x = pdf.MARGIN_X
    value_x = left_x + label_col_w

    # Organization header matching the Mukisa Foundation requisition template
    pdf.text(left_x, pdf.y, "MUKISA FOUNDATION", font="F2", size=15)
    pdf.text(left_x + 175, pdf.y, "...hope for children with special needs.", font="F1", size=11)
    pdf.y -= 14
    pdf.text(
        left_x,
        pdf.y,
        "Kalema Road, Lungujja | PO Box 34284, Kampala | info@mukisafoundation.org",
        font="F1",
        size=8.5,
    )
    pdf.y -= 10
    # Accent bars
    pdf.rect(left_x, pdf.y, table_width, 3, stroke=False, fill_rgb=(0.82, 0.16, 0.16))
    pdf.y -= 4
    pdf.rect(left_x, pdf.y, table_width, 3, stroke=False, fill_rgb=(0.95, 0.78, 0.12))
    pdf.y -= 18

    # Title banner
    pdf.text(
        left_x,
        pdf.y,
        f"FUND REQUISITION FORM  (REQ #{requisition.pk})",
        font="F2",
        size=12,
    )
    pdf.text(
        left_x + 310,
        pdf.y,
        f"Status: {requisition.get_status_display()}",
        font="F2",
        size=10,
    )
    pdf.y -= 14
    pdf.text(
        left_x,
        pdf.y,
        f"ACTIVITY: {requisition.title} ({requisition.get_activity_type_display()})",
        font="F2",
        size=10.5,
    )
    pdf.y -= 10

    def draw_table_row(label: str, value: str, shaded: bool = False):
        label_lines = _wrap_lines(label, 22)
        value_lines = _wrap_lines(value, 62)
        line_count = max(len(label_lines), len(value_lines), 1)
        row_h = max(line_count * 13.0 + 8.0, 22.0)
        pdf.ensure_space(row_h)
        top = pdf.y
        bottom = top - row_h
        fill = (0.91, 0.96, 0.98) if shaded else None
        pdf.rect(left_x, bottom, label_col_w, row_h, stroke=True, fill_rgb=None)
        pdf.rect(value_x, bottom, value_col_w, row_h, stroke=True, fill_rgb=fill)
        for idx, l_text in enumerate(label_lines):
            pdf.text(left_x + 5, top - 13 - idx * 13, l_text, font="F2", size=9.0)
        for idx, v_text in enumerate(value_lines):
            pdf.text(value_x + 6, top - 13 - idx * 13, v_text, font="F1", size=9.0)
        pdf.y = bottom

    requester_name = requisition.requestor.get_full_name() or requisition.requestor.username
    draw_table_row("Requester:", requester_name)
    draw_table_row("Department:", requisition.department or "—")
    draw_table_row(
        "Requested Date:",
        timezone.localtime(requisition.requested_at).strftime("%Y-%m-%d %H:%M")
        if requisition.requested_at
        else "—",
    )
    if requisition.voucher_number or requisition.disbursement_method:
        draw_table_row(
            "Voucher / Payment:",
            f"{requisition.voucher_number or '—'} ({requisition.get_disbursement_method_display() or '—'})",
        )
    draw_table_row("Description / Purpose:", requisition.description or "—")

    if not requisition.is_administrative:
        draw_table_row(
            "Date of the Activity:",
            requisition.activity_date.strftime("%Y-%m-%d")
            if requisition.activity_date
            else "—",
        )
        draw_table_row("Team lead:", requisition.team_lead or "—")
        draw_table_row("Impact", requisition.impact or "—", shaded=True)
        draw_table_row("Program:", requisition.program or "—", shaded=True)
        draw_table_row("Indicator:", requisition.indicator or "—", shaded=True)
        draw_table_row("Overview of the Activity", requisition.activity_overview or "—")
        draw_table_row("Expected outcomes", requisition.expected_outcomes or "—")
        draw_table_row("Expected Outputs", requisition.expected_outputs or "—")
        draw_table_row("Target Number", requisition.target_number or "—")
        draw_table_row("Inputs", requisition.inputs or "—")
        draw_table_row(
            "Other staff who will be involved in your activity",
            requisition.other_staff or "—",
        )

    # Budget table section
    items = list(requisition.items.all())
    header_h = 20.0
    row_h = 18.0
    total_budget_rows = max(len(items), 1) + 2
    budget_block_h = header_h + total_budget_rows * row_h + 12.0
    pdf.ensure_space(min(budget_block_h, 140.0))

    col_item_w = value_col_w * 0.44
    col_qty_w = value_col_w * 0.14
    col_unit_w = value_col_w * 0.20
    col_total_w = value_col_w - col_item_w - col_qty_w - col_unit_w

    x_item = value_x
    x_qty = x_item + col_item_w
    x_unit = x_qty + col_qty_w
    x_total = x_unit + col_unit_w

    # Budget header row
    top = pdf.y
    bottom = top - header_h
    pdf.rect(left_x, bottom, label_col_w, header_h, stroke=True)
    pdf.text(left_x + 5, top - 13, "Budget", font="F2", size=9.5)
    for col_x, col_w, title in (
        (x_item, col_item_w, "ITEM"),
        (x_qty, col_qty_w, "QTY"),
        (x_unit, col_unit_w, "UNIT COST"),
        (x_total, col_total_w, f"TOTAL ({requisition.currency})"),
    ):
        pdf.rect(col_x, bottom, col_w, header_h, stroke=True, fill_rgb=(0.86, 0.85, 0.78))
        pdf.text(col_x + 4, top - 13, title, font="F2", size=8.5)
    pdf.y = bottom

    if items:
        for item in items:
            pdf.ensure_space(row_h)
            top = pdf.y
            bottom = top - row_h
            pdf.rect(left_x, bottom, label_col_w, row_h, stroke=True)
            pdf.rect(x_item, bottom, col_item_w, row_h, stroke=True)
            pdf.rect(x_qty, bottom, col_qty_w, row_h, stroke=True)
            pdf.rect(x_unit, bottom, col_unit_w, row_h, stroke=True)
            pdf.rect(x_total, bottom, col_total_w, row_h, stroke=True)
            pdf.text(x_item + 4, top - 12, item.item_description[:32], font="F1", size=8.5)
            pdf.text(x_qty + 4, top - 12, f"{item.quantity:,.2f}", font="F1", size=8.5)
            pdf.text(x_unit + 4, top - 12, f"{item.unit_cost:,.2f}", font="F1", size=8.5)
            pdf.text(x_total + 4, top - 12, f"{item.total_cost:,.2f}", font="F1", size=8.5)
            pdf.y = bottom
    else:
        pdf.ensure_space(row_h)
        top = pdf.y
        bottom = top - row_h
        pdf.rect(left_x, bottom, label_col_w, row_h, stroke=True)
        pdf.rect(x_item, bottom, value_col_w, row_h, stroke=True)
        pdf.text(x_item + 4, top - 12, "Lump-sum budget request", font="F1", size=8.5)
        pdf.y = bottom

    # Total row
    pdf.ensure_space(row_h)
    top = pdf.y
    bottom = top - row_h
    pdf.rect(left_x, bottom, label_col_w, row_h, stroke=True)
    pdf.rect(
        x_item,
        bottom,
        col_item_w + col_qty_w + col_unit_w,
        row_h,
        stroke=True,
        fill_rgb=(0.94, 0.94, 0.94),
    )
    pdf.rect(x_total, bottom, col_total_w, row_h, stroke=True, fill_rgb=(0.94, 0.94, 0.94))
    pdf.text(x_item + 4, top - 12, "TOTAL BUDGET", font="F2", size=9.0)
    pdf.text(
        x_total + 4,
        top - 12,
        f"{requisition.currency} {requisition.amount:,.2f}",
        font="F2",
        size=9.0,
    )
    pdf.y = bottom

    if not requisition.is_administrative:
        draw_table_row(
            "Risk assessment plan and mitigation for the planned activity",
            requisition.risk_assessment or "—",
        )
        draw_table_row("Reporting strategy", requisition.reporting_strategy or "—")

    # Approval & sign-off trail for filing
    pdf.y -= 18
    pdf.ensure_space(95.0)
    logs = list(requisition.logs.select_related("actor").order_by("timestamp"))

    def latest_actor_for(statuses):
        for log in reversed(logs):
            if log.action == "APPROVE" and log.previous_status in statuses:
                name = log.actor.get_full_name() or log.actor.username
                dt = timezone.localtime(log.timestamp).strftime("%Y-%m-%d")
                return name, dt
        return "........................................", "...................."

    req_date = (
        timezone.localtime(requisition.requested_at).strftime("%Y-%m-%d")
        if requisition.requested_at
        else "...................."
    )
    reviewed_name, reviewed_date = latest_actor_for(
        {"PENDING_SUPERVISOR", "PENDING_ME", "PENDING_PROGRAM_LEAD"}
    )
    approved_name, approved_date = latest_actor_for(
        {"PENDING_EXECUTIVE_DIRECTOR", "PENDING_FINANCE"}
    )

    pdf.text(
        left_x,
        pdf.y,
        f"Prepared by: {requester_name}    Signature: Signed electronically    Date: {req_date}",
        font="F1",
        size=9.5,
    )
    pdf.y -= 20
    pdf.text(
        left_x,
        pdf.y,
        f"Reviewed by: {reviewed_name}    Signature: ....................    Date: {reviewed_date}",
        font="F1",
        size=9.5,
    )
    pdf.y -= 20
    pdf.text(
        left_x,
        pdf.y,
        f"Approved by: {approved_name}    Signature: ....................    Date: {approved_date}",
        font="F1",
        size=9.5,
    )

    return pdf.to_bytes()
