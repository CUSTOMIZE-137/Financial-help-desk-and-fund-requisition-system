"""Dynamic PDF generator matching the official Mukisa Foundation requisition template."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import re
import struct
import textwrap
import zlib

from django.utils import timezone

TEMPLATE_PDF_PATH = Path(__file__).resolve().parent / "Requisition_template.pdf"


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
    raw = str(text or "").strip()
    if not raw:
        return [""]
    lines: list[str] = []
    for paragraph in raw.splitlines():
        wrapped = textwrap.wrap(paragraph, width=width) if paragraph.strip() else [""]
        lines.extend(wrapped)
    return lines or [""]


def _display_actor_name(user) -> str:
    """Return a clean human-readable name for a user on printed forms."""
    if user is None:
        return ""
    role = getattr(user, "role", "")
    full_name = (user.get_full_name() or "").strip()
    if role == "PROGRAM_LEAD" and full_name.lower() in (
        "",
        "program director",
        "executive director",
        "program lead",
        "program_director",
        "program_lead",
    ):
        return "Program Lead"
    if full_name:
        return full_name
    if hasattr(user, "get_role_display") and role:
        return user.get_role_display()
    username = (user.username or "").strip()
    return username.replace("_", " ").replace("-", " ").title()


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _parse_image_for_pdf(
    raw_bytes: bytes,
) -> tuple[int, int, bytes, bytes | None, str, str] | None:
    """Parse a PNG or JPEG image into PDF XObject stream data.

    Returns ``(width, height, image_stream, smask_stream_or_none, filter_name, color_space)``
    or ``None`` if the image cannot be decoded.
    """
    if not raw_bytes:
        return None

    # JPEG (DCTDecode)
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        pos = 2
        length = len(raw_bytes)
        while pos < length - 1:
            if raw_bytes[pos] != 0xFF:
                pos += 1
                continue
            while pos < length and raw_bytes[pos] == 0xFF:
                pos += 1
            if pos >= length:
                break
            marker = raw_bytes[pos]
            pos += 1
            if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7):
                continue
            if pos + 2 > length:
                break
            seg_len = struct.unpack(">H", raw_bytes[pos : pos + 2])[0]
            if marker in (0xC0, 0xC1, 0xC2) and pos + 7 <= length:
                height, width = struct.unpack(">HH", raw_bytes[pos + 3 : pos + 7])
                components = raw_bytes[pos + 7]
                color_space = (
                    "/DeviceGray"
                    if components == 1
                    else ("/DeviceCMYK" if components == 4 else "/DeviceRGB")
                )
                if width > 0 and height > 0:
                    return (width, height, raw_bytes, None, "/DCTDecode", color_space)
                return None
            pos += seg_len
        return None

    # PNG (FlateDecode)
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        pos = 8
        length = len(raw_bytes)
        width = height = bit_depth = color_type = interlace = 0
        plte = b""
        trns = b""
        idat_parts: list[bytes] = []
        while pos + 8 <= length:
            chunk_len = struct.unpack(">I", raw_bytes[pos : pos + 4])[0]
            chunk_type = raw_bytes[pos + 4 : pos + 8]
            chunk_data = raw_bytes[pos + 8 : pos + 8 + chunk_len]
            pos += 12 + chunk_len
            if chunk_type == b"IHDR" and len(chunk_data) >= 13:
                width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                    ">IIBBBBB", chunk_data[:13]
                )
            elif chunk_type == b"PLTE":
                plte = chunk_data
            elif chunk_type == b"tRNS":
                trns = chunk_data
            elif chunk_type == b"IDAT":
                idat_parts.append(chunk_data)
            elif chunk_type == b"IEND":
                break

        if width <= 0 or height <= 0 or bit_depth != 8 or interlace != 0:
            return None
        bpp_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
        if color_type not in bpp_map:
            return None
        bpp = bpp_map[color_type]
        row_len = width * bpp
        try:
            decompressed = zlib.decompress(b"".join(idat_parts))
        except zlib.error:
            return None
        if len(decompressed) < height * (1 + row_len):
            return None

        prev_row = bytearray(row_len)
        rgb_out = bytearray()
        alpha_out = bytearray()
        has_alpha = color_type in (4, 6) or (color_type == 3 and bool(trns))

        offset = 0
        for _ in range(height):
            filt = decompressed[offset]
            offset += 1
            cur = bytearray(decompressed[offset : offset + row_len])
            offset += row_len
            if filt == 1:
                for i in range(bpp, row_len):
                    cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
            elif filt == 2:
                for i in range(row_len):
                    cur[i] = (cur[i] + prev_row[i]) & 0xFF
            elif filt == 3:
                for i in range(row_len):
                    a = cur[i - bpp] if i >= bpp else 0
                    b = prev_row[i]
                    cur[i] = (cur[i] + ((a + b) >> 1)) & 0xFF
            elif filt == 4:
                for i in range(row_len):
                    a = cur[i - bpp] if i >= bpp else 0
                    b = prev_row[i]
                    c = prev_row[i - bpp] if i >= bpp else 0
                    cur[i] = (cur[i] + _paeth_predictor(a, b, c)) & 0xFF
            prev_row = cur

            if color_type == 2:
                rgb_out.extend(cur)
            elif color_type == 6:
                for px in range(0, row_len, 4):
                    rgb_out.extend(cur[px : px + 3])
                    alpha_out.append(cur[px + 3])
            elif color_type == 0:
                for g in cur:
                    rgb_out.extend((g, g, g))
            elif color_type == 4:
                for px in range(0, row_len, 2):
                    g = cur[px]
                    rgb_out.extend((g, g, g))
                    alpha_out.append(cur[px + 1])
            elif color_type == 3:
                for idx in cur:
                    p_off = idx * 3
                    if p_off + 3 <= len(plte):
                        rgb_out.extend(plte[p_off : p_off + 3])
                    else:
                        rgb_out.extend((0, 0, 0))
                    if has_alpha:
                        alpha_out.append(trns[idx] if idx < len(trns) else 255)

        img_compressed = zlib.compress(bytes(rgb_out))
        smask_compressed = zlib.compress(bytes(alpha_out)) if has_alpha else None
        return (
            width,
            height,
            img_compressed,
            smask_compressed,
            "/FlateDecode",
            "/DeviceRGB",
        )

    return None


def _read_user_signature_bytes(user) -> bytes | None:
    """Safely read a user's saved ``signature_image`` bytes if present."""
    field = getattr(user, "signature_image", None)
    if not field:
        return None
    name = (getattr(field, "name", "") or "").lower()
    if any(
        sample in name
        for sample in ("signature1.jpg", "signature2.jpg", "signature3.jpg")
    ):
        return None
    try:
        with field.open("rb") as handle:
            return handle.read()
    except Exception:
        return None


@lru_cache(maxsize=1)
def _extract_template_header_objects() -> tuple[bytes, bytes, bytes] | None:
    """Extract the Mukisa Foundation letterhead XObject (objs 7, 8, 9) from ``Requisition_template.pdf``."""
    if not TEMPLATE_PDF_PATH.exists():
        return None
    data = TEMPLATE_PDF_PATH.read_bytes()
    found: dict[int, bytes] = {}
    for oid in (7, 8, 9):
        match = re.search(
            rf"(?:^|\r?\n){oid}\s+0\s+obj\r?\n(.*?)\r?\nendobj".encode("latin-1"),
            data,
            re.S,
        )
        if not match:
            return None
        found[oid] = match.group(1)
    return found[7], found[8], found[9]


class _DynamicRequisitionPDF:
    """Multi-page PDF builder that matches ``Requisition_template.pdf`` with dynamic row sizing."""

    PAGE_WIDTH = 612.0
    PAGE_HEIGHT = 792.0
    LEFT_X = 72.0
    LABEL_COL_W = 112.0
    VALUE_X = 184.0
    VALUE_COL_W = 362.0
    TABLE_W = 474.0
    TOP_Y = 744.0
    BOTTOM_Y = 28.0

    # Template colors from Requisition_template.pdf
    BLUE_SHADE = (0.8549, 0.9333, 0.9529)
    BUDGET_SHADE = (0.8667, 0.8510, 0.7686)
    BUDGET_TOTAL_SHADE = (0.8200, 0.8020, 0.7100)

    def __init__(self):
        self.pages: list[list[str]] = [[]]
        self.y = self.TOP_Y
        self.sig_xobjects: dict[
            str, tuple[int, int, bytes, bytes | None, str, str]
        ] = {}

    @property
    def cmds(self) -> list[str]:
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
        size: float = 9.0,
        rgb: tuple[float, float, float] | None = None,
    ):
        escaped = _pdf_escape(value)
        if rgb is not None:
            r, g, b = rgb
            self.cmds.append(
                f"q {r:.3f} {g:.3f} {b:.3f} rg BT /{font} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET Q"
            )
        else:
            self.cmds.append(
                f"BT /{font} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({escaped}) Tj ET"
            )

    def text_right(
        self,
        right_x: float,
        y: float,
        value: str,
        font: str = "F1",
        size: float = 8.5,
    ):
        approx_w = len(str(value or "")) * size * (0.54 if font == "F2" else 0.50)
        self.text(max(right_x - approx_w, 0.0), y, value, font=font, size=size)

    def text_center(
        self,
        center_x: float,
        y: float,
        value: str,
        font: str = "F1",
        size: float = 8.5,
    ):
        approx_w = len(str(value or "")) * size * (0.54 if font == "F2" else 0.50)
        self.text(center_x - approx_w / 2.0, y, value, font=font, size=size)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        stroke: bool = True,
        fill_rgb: tuple[float, float, float] | None = None,
        line_width: float = 0.85,
    ):
        ops = ["q"]
        if fill_rgb is not None:
            r, g, b = fill_rgb
            ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        ops.append(f"0 0 0 RG {line_width:.2f} w")
        ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re")
        if fill_rgb is not None and stroke:
            ops.append("B")
        elif fill_rgb is not None:
            ops.append("f")
        else:
            ops.append("S")
        ops.append("Q")
        self.cmds.append(" ".join(ops))

    def draw_header(self, title: str):
        """Draw the Mukisa Foundation letterhead banner and centered ACTIVITY title."""
        self.cmds.append("q 454.50 0 0 112.00 67.50 676.00 cm /XHeader Do Q")
        self.y = 657.0
        activity_str = (title or "").strip()
        label = "ACTIVITY: "
        full_heading = f"{label}{activity_str}"
        center_x = self.LEFT_X + self.TABLE_W / 2.0
        est_w = len(full_heading) * 5.2
        start_x = max(self.LEFT_X, center_x - est_w / 2.0)
        self.text(start_x, self.y, label, font="F1", size=10.0)
        self.text(start_x + 52.0, self.y, activity_str, font="F2", size=10.0)
        self.y -= 6.0

    def draw_field_row(
        self,
        label_lines: list[str],
        value: str,
        shaded: bool = False,
        min_height: float = 16.0,
    ):
        """Draw one 2-column row of the main requisition table, expanding if text wraps."""
        val_lines = _wrap_lines(value, 68)
        line_count = max(len(label_lines), len(val_lines), 1)
        line_h = 10.2
        row_h = max(line_count * line_h + 5.5, min_height)
        self.ensure_space(row_h)

        top = self.y
        bottom = top - row_h
        fill = self.BLUE_SHADE if shaded else None
        self.rect(self.LEFT_X, bottom, self.LABEL_COL_W, row_h, stroke=True, fill_rgb=fill)
        self.rect(self.VALUE_X, bottom, self.VALUE_COL_W, row_h, stroke=True, fill_rgb=fill)

        for idx, l_text in enumerate(label_lines):
            self.text(
                self.LEFT_X + 5.5,
                top - 10.5 - idx * line_h,
                l_text,
                font="F1",
                size=8.6,
            )
        for idx, v_text in enumerate(val_lines):
            if v_text:
                self.text(
                    self.VALUE_X + 6.0,
                    top - 10.5 - idx * line_h,
                    v_text,
                    font="F1",
                    size=8.6,
                )
        self.y = bottom

    def draw_budget_section(self, requisition):
        """Draw the outer Budget row and nested itemized table sized to the actual items."""
        items = list(requisition.items.all())
        prepared_rows: list[tuple[list[str], str, str, str]] = []
        if items:
            for item in items:
                desc_lines = _wrap_lines(item.item_description, 32)
                qty_str = (
                    f"{item.quantity:,.0f}"
                    if item.quantity == int(item.quantity)
                    else f"{item.quantity:,.2f}"
                )
                prepared_rows.append(
                    (
                        desc_lines,
                        qty_str,
                        f"{item.unit_cost:,.2f}",
                        f"{item.total_cost:,.2f}",
                    )
                )
        else:
            prepared_rows.append(
                (
                    _wrap_lines(requisition.title or "Activity budget", 32),
                    "1",
                    f"{requisition.amount:,.2f}",
                    f"{requisition.amount:,.2f}",
                )
            )

        inner_pad_x = 5.5
        inner_pad_top = 8.0
        inner_pad_bottom = 8.0
        inner_x = self.VALUE_X + inner_pad_x
        inner_w = self.VALUE_COL_W - 2 * inner_pad_x  # 351.0 pt

        col_item_w = 148.0
        col_qty_w = 46.0
        col_unit_w = 72.0
        col_total_w = inner_w - col_item_w - col_qty_w - col_unit_w  # 85.0 pt

        x_item = inner_x
        x_qty = x_item + col_item_w
        x_unit = x_qty + col_qty_w
        x_total = x_unit + col_unit_w

        header_h = 18.0
        total_row_h = 18.0

        row_heights = [
            max(len(desc_lines) * 10.2 + 6.0, 16.5)
            for desc_lines, _, _, _ in prepared_rows
        ]

        # Ensure at least the Budget header + first item row fit on the current page
        min_start_h = inner_pad_top + header_h + row_heights[0] + inner_pad_bottom
        self.ensure_space(min_start_h)

        idx = 0
        first_chunk = True
        num_rows = len(prepared_rows)

        while idx < num_rows:
            avail_y = self.y - self.BOTTOM_Y
            chunk_indices: list[int] = []
            used_inner_h = inner_pad_top + (header_h if first_chunk else 0.0)

            while idx < num_rows:
                rh = row_heights[idx]
                is_last = idx == num_rows - 1
                extra_after = (total_row_h if is_last else 0.0) + inner_pad_bottom
                if chunk_indices and (used_inner_h + rh + extra_after > avail_y):
                    break
                chunk_indices.append(idx)
                used_inner_h += rh
                idx += 1

            include_total = idx == num_rows
            if include_total:
                used_inner_h += total_row_h
            outer_h = used_inner_h + inner_pad_bottom

            top = self.y
            bottom = top - outer_h

            # Draw outer Budget row cells
            self.rect(self.LEFT_X, bottom, self.LABEL_COL_W, outer_h, stroke=True)
            self.rect(self.VALUE_X, bottom, self.VALUE_COL_W, outer_h, stroke=True)
            if first_chunk:
                self.text(self.LEFT_X + 5.5, top - 12.0, "Budget", font="F1", size=8.8)

            cur_y = top - inner_pad_top

            # Draw inner table column headers on the first chunk
            if first_chunk:
                h_bottom = cur_y - header_h
                for cx, cw, title in (
                    (x_item, col_item_w, "ITEM"),
                    (x_qty, col_qty_w, "QTY"),
                    (x_unit, col_unit_w, "UNIT COST"),
                    (x_total, col_total_w, f"TOTAL COST ({requisition.currency.title()})"),
                ):
                    self.rect(
                        cx,
                        h_bottom,
                        cw,
                        header_h,
                        stroke=True,
                        fill_rgb=self.BUDGET_SHADE,
                        line_width=0.75,
                    )
                    self.text_center(cx + cw / 2.0, cur_y - 13.0, title, font="F1", size=8.5)
                cur_y = h_bottom

            # Draw each budget item row in this chunk
            for r_idx in chunk_indices:
                desc_lines, qty_str, unit_str, total_str = prepared_rows[r_idx]
                rh = row_heights[r_idx]
                r_bottom = cur_y - rh
                for cx, cw in (
                    (x_item, col_item_w),
                    (x_qty, col_qty_w),
                    (x_unit, col_unit_w),
                    (x_total, col_total_w),
                ):
                    self.rect(
                        cx,
                        r_bottom,
                        cw,
                        rh,
                        stroke=True,
                        fill_rgb=self.BUDGET_SHADE,
                        line_width=0.75,
                    )
                for l_idx, d_line in enumerate(desc_lines):
                    self.text(
                        x_item + 5.0,
                        cur_y - 12.0 - l_idx * 10.8,
                        d_line,
                        font="F1",
                        size=8.4,
                    )
                mid_text_y = cur_y - 12.0
                self.text_center(x_qty + col_qty_w / 2.0, mid_text_y, qty_str, font="F1", size=8.4)
                self.text_right(x_unit + col_unit_w - 5.0, mid_text_y, unit_str, font="F1", size=8.4)
                self.text_right(x_total + col_total_w - 5.0, mid_text_y, total_str, font="F1", size=8.4)
                cur_y = r_bottom

            # Draw TOTAL row at the end of the budget items
            if include_total:
                t_bottom = cur_y - total_row_h
                for cx, cw in (
                    (x_item, col_item_w),
                    (x_qty, col_qty_w),
                    (x_unit, col_unit_w),
                    (x_total, col_total_w),
                ):
                    self.rect(
                        cx,
                        t_bottom,
                        cw,
                        total_row_h,
                        stroke=True,
                        fill_rgb=self.BUDGET_TOTAL_SHADE,
                        line_width=0.75,
                    )
                self.text(x_item + 5.0, cur_y - 13.0, "TOTAL", font="F2", size=8.8)
                self.text_right(
                    x_total + col_total_w - 5.0,
                    cur_y - 13.0,
                    f"{requisition.currency} {requisition.amount:,.2f}",
                    font="F2",
                    size=8.8,
                )
                cur_y = t_bottom

            self.y = bottom
            first_chunk = False
            if idx < num_rows:
                self.new_page()

    def dotted_line(self, x1: float, x2: float, y: float):
        """Draw a crisp vector dotted line for signature and date fields."""
        self.cmds.append(
            f"q 0.25 0.25 0.25 RG 0.65 w [1.0 1.8] 0 d {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S Q"
        )

    def draw_cursive_signature(
        self, x1: float, x2: float, baseline_y: float, actor_name: str
    ):
        """Render a stylized handwritten cursive e-signature with a pen flourish matching the user."""
        clean_name = actor_name.strip()
        if not clean_name:
            return
        avail_w = x2 - x1 - 6.0
        est_size = min(10.2, max(7.4, avail_w / max(len(clean_name) * 0.50, 1.0)))
        text_w = min(avail_w, len(clean_name) * est_size * 0.48)
        start_x = x1 + max(3.0, (x2 - x1 - text_w) / 2.0)
        text_y = baseline_y + 1.5
        escaped = _pdf_escape(clean_name)
        # Slanted calligraphic text matrix + fountain-pen navy ink
        self.cmds.append(
            f"q 0.050 0.165 0.460 rg BT /F3 {est_size:.2f} Tf "
            f"1 0.035 0.18 1 {start_x:.2f} {text_y:.2f} Tm ({escaped}) Tj ET Q"
        )
        # Subtle curved pen flourish sweeping beneath the signature
        f_x0 = start_x - 1.5
        f_y0 = baseline_y - 0.8
        f_x1 = start_x + text_w * 0.38
        f_y1 = baseline_y - 3.2
        f_x2 = start_x + text_w * 0.78
        f_y2 = baseline_y + 0.6
        f_x3 = start_x + text_w + 3.0
        f_y3 = baseline_y + 1.8
        self.cmds.append(
            f"q 0.050 0.165 0.460 RG 0.60 w 1 J "
            f"{f_x0:.2f} {f_y0:.2f} m {f_x1:.2f} {f_y1:.2f} {f_x2:.2f} {f_y2:.2f} {f_x3:.2f} {f_y3:.2f} c S Q"
        )

    def draw_signatures(self, requisition):
        """Draw the Prepared by / Reviewed by / Approved by dotted lines and stamp e-signatures."""
        block_height = 84.0
        self.ensure_space(block_height)
        self.y -= 18.0

        logs = list(requisition.logs.select_related("actor").order_by("timestamp"))

        def latest_actor_for(statuses):
            for log in reversed(logs):
                if log.action == "APPROVE" and log.previous_status in statuses:
                    name = _display_actor_name(log.actor)
                    dt = timezone.localtime(log.timestamp).strftime("%Y-%m-%d")
                    return log.actor, name, dt
            return None, "", ""

        requester_name = _display_actor_name(requisition.requestor)
        req_date = (
            timezone.localtime(requisition.requested_at).strftime("%Y-%m-%d")
            if requisition.requested_at
            else ""
        )
        reviewed_actor, reviewed_name, reviewed_date = latest_actor_for(
            {"PENDING_PROGRAM_LEAD"}
        )
        if not reviewed_actor:
            reviewed_actor, reviewed_name, reviewed_date = latest_actor_for(
                {"PENDING_SUPERVISOR", "PENDING_ME"}
            )
        if reviewed_name.lower() in ("program director", "executive director"):
            reviewed_name = "Program Lead"
        approved_actor, approved_name, approved_date = latest_actor_for(
            {"PENDING_EXECUTIVE_DIRECTOR"}
        )
        if not approved_actor:
            approved_actor, approved_name, approved_date = latest_actor_for(
                {"PENDING_FINANCE"}
            )

        rows = (
            ("SigPrep", "Prepared by:", "Signature:", requisition.requestor, requester_name, req_date),
            ("SigRev", "Reviewed by:", "Signature:", reviewed_actor, reviewed_name, reviewed_date),
            ("SigApp", "Approved by:", "Signature:", approved_actor, approved_name, approved_date),
        )

        for slot_name, role_label, sig_label, actor, actor_name, date_str in rows:
            baseline_y = self.y
            # Draw template labels and fine vector dotted lines
            self.text(self.LEFT_X, baseline_y, role_label, font="F1", size=9.2)
            self.dotted_line(134.0, 306.0, baseline_y - 1.0)
            self.text(312.0, baseline_y, sig_label, font="F1", size=9.2)
            self.dotted_line(358.0, 444.0, baseline_y - 1.0)
            self.text(450.0, baseline_y, "Date:", font="F1", size=9.2)
            self.dotted_line(476.0, 546.0, baseline_y - 1.0)

            if actor_name:
                self.text(138.0, baseline_y + 1.0, actor_name, font="F1", size=9.2)
            if date_str:
                self.text(480.0, baseline_y + 1.0, date_str, font="F1", size=8.8)

            parsed_sig = None
            if actor is not None:
                raw_sig = _read_user_signature_bytes(actor)
                if raw_sig:
                    parsed_sig = _parse_image_for_pdf(raw_sig)

            if parsed_sig is not None:
                self.sig_xobjects[slot_name] = parsed_sig
                img_w, img_h = parsed_sig[0], parsed_sig[1]
                max_w, max_h = 80.0, 42.0
                scale = min(max_w / max(img_w, 1), max_h / max(img_h, 1))
                draw_w = img_w * scale
                draw_h = img_h * scale
                draw_x = 358.0 + (86.0 - draw_w) / 2.0
                draw_y = baseline_y + 3.0 - (draw_h / 2.0)
                self.cmds.append(
                    f"q /GSig gs {draw_w:.2f} 0 0 {draw_h:.2f} {draw_x:.2f} {draw_y:.2f} cm /{slot_name} Do Q"
                )
            elif actor_name:
                self.draw_cursive_signature(358.0, 444.0, baseline_y, actor_name)

            self.y -= 26.0

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
        font_italic_id = add_obj(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-BoldItalic /Encoding /WinAnsiEncoding >>"
        )
        gs_sig_id = add_obj(
            b"<< /Type /ExtGState /BM /Multiply /CA 1 /ca 1 >>"
        )

        xobject_entries: list[str] = []
        header_objs = _extract_template_header_objects()
        if header_objs is not None:
            raw_obj7, raw_obj8, raw_obj9 = header_objs
            icc_id = add_obj(raw_obj8)
            smask_id = add_obj(raw_obj9)
            updated_obj7 = (
                raw_obj7.replace(
                    b"/ColorSpace [/ICCBased 8 0 R]",
                    f"/ColorSpace [/ICCBased {icc_id} 0 R]".encode("latin-1"),
                ).replace(
                    b"/SMask 9 0 R",
                    f"/SMask {smask_id} 0 R".encode("latin-1"),
                )
            )
            header_id = add_obj(updated_obj7)
            xobject_entries.append(f"/XHeader {header_id} 0 R")

        for slot_name, (
            width,
            height,
            img_stream,
            smask_stream,
            filter_name,
            color_space,
        ) in self.sig_xobjects.items():
            smask_entry = ""
            if smask_stream is not None:
                sig_smask_id = add_obj(
                    (
                        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                        f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                        f"/Length {len(smask_stream)} >>\nstream\n"
                    ).encode("latin-1")
                    + smask_stream
                    + b"\nendstream"
                )
                smask_entry = f" /SMask {sig_smask_id} 0 R"

            if color_space == "/DeviceRGB" and smask_stream is None:
                decode_entry = " /Decode [-0.22 1.14 -0.22 1.14 -0.22 1.14]"
            elif color_space == "/DeviceGray" and smask_stream is None:
                decode_entry = " /Decode [-0.22 1.14]"
            else:
                decode_entry = ""

            sig_img_id = add_obj(
                (
                    f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                    f"/ColorSpace {color_space} /BitsPerComponent 8 /Filter {filter_name} "
                    f"/Interpolate true{decode_entry}{smask_entry} /Length {len(img_stream)} >>\nstream\n"
                ).encode("latin-1")
                + img_stream
                + b"\nendstream"
            )
            xobject_entries.append(f"/{slot_name} {sig_img_id} 0 R")

        pages_id_placeholder = add_obj(b"")
        page_ids: list[int] = []
        xobj_dict = f"/XObject << {' '.join(xobject_entries)} >> " if xobject_entries else ""

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
                f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R /F3 {font_italic_id} 0 R >> "
                f"/ExtGState << /GSig {gs_sig_id} 0 R >> "
                f"{xobj_dict}>> "
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
    """Build a printable Mukisa Foundation requisition PDF with dynamic budget sizing."""
    pdf = _DynamicRequisitionPDF()
    pdf.draw_header(requisition.title or "")

    activity_date_str = (
        requisition.activity_date.strftime("%Y-%m-%d")
        if requisition.activity_date
        else (
            timezone.localtime(requisition.requested_at).strftime("%Y-%m-%d")
            if requisition.requested_at
            else ""
        )
    )
    requester_name = _display_actor_name(requisition.requestor)
    team_lead_str = requisition.team_lead or requester_name
    program_str = requisition.program or requisition.get_activity_type_display()
    overview_str = requisition.activity_overview or requisition.description or ""

    pdf.draw_field_row(["Date of the Activity:"], activity_date_str, min_height=16.0)
    pdf.draw_field_row(["Team lead:"], team_lead_str, min_height=16.0)
    pdf.draw_field_row(["Impact"], requisition.impact or "", shaded=True, min_height=20.0)
    pdf.draw_field_row(["Program:"], program_str, shaded=True, min_height=16.0)
    pdf.draw_field_row(["Indicator:"], requisition.indicator or "", shaded=True, min_height=22.0)
    pdf.draw_field_row(["Overview of the", "Activity"], overview_str, min_height=24.0)
    pdf.draw_field_row(["Expected outcomes"], requisition.expected_outcomes or "", min_height=22.0)
    pdf.draw_field_row(["Expected Outputs"], requisition.expected_outputs or "", min_height=20.0)
    pdf.draw_field_row(["Target Number"], requisition.target_number or "", min_height=16.0)
    pdf.draw_field_row(["Inputs"], requisition.inputs or "", min_height=20.0)
    pdf.draw_field_row(
        ["Other staff who will be", "involved in your", "activity"],
        requisition.other_staff or "",
        min_height=34.0,
    )

    # Dynamic Budget table sized to the exact number of items in the budget
    pdf.draw_budget_section(requisition)

    pdf.draw_field_row(
        ["Risk assessment plan", "and mitigation for the", "planned activity"],
        requisition.risk_assessment or "",
        min_height=34.0,
    )
    pdf.draw_field_row(
        ["Reporting strategy"],
        requisition.reporting_strategy or "",
        min_height=20.0,
    )

    pdf.draw_signatures(requisition)
    return pdf.to_bytes()
