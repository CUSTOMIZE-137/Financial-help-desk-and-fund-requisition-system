"""Microsoft Teams app manifest generation, validation, and ZIP package builder."""

from __future__ import annotations

import json
from pathlib import Path
import struct
from typing import Any
from urllib.parse import urlparse
import uuid
import zipfile
import zlib

from django.conf import settings
from django.core.exceptions import ValidationError

TEAMS_DIR = Path(settings.BASE_DIR) / "teams"
MANIFEST_TEMPLATE_PATH = TEAMS_DIR / "manifest.template.json"
DEFAULT_COLOR_ICON_PATH = TEAMS_DIR / "color.png"
DEFAULT_OUTLINE_ICON_PATH = TEAMS_DIR / "outline.png"


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value).strip())
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def extract_https_domain(site_url: str, *, require_https: bool = True) -> str:
    """Extract and validate the hostname from a site URL for Teams manifest usage."""
    parsed = urlparse((site_url or "").strip())
    if require_https and parsed.scheme.lower() != "https":
        raise ValidationError("Microsoft Teams app manifests require an https:// SITE_URL.")
    if not parsed.netloc:
        raise ValidationError("SITE_URL must include a valid hostname.")
    return parsed.netloc


def build_teams_manifest(
    *,
    app_id: str | None = None,
    client_id: str | None = None,
    site_url: str | None = None,
    require_https: bool = True,
) -> dict[str, Any]:
    """Render the Teams manifest template with validated organization identifiers."""
    resolved_app_id = (
        app_id if app_id is not None else getattr(settings, "MICROSOFT_TEAMS_APP_ID", "")
    ).strip()
    resolved_client_id = (
        client_id if client_id is not None else getattr(settings, "MICROSOFT_CLIENT_ID", "")
    ).strip()
    resolved_site_url = (
        site_url if site_url is not None else getattr(settings, "SITE_URL", "")
    ).strip()

    if not _is_valid_uuid(resolved_app_id):
        raise ValidationError("MICROSOFT_TEAMS_APP_ID must be a valid UUID.")
    if not _is_valid_uuid(resolved_client_id):
        raise ValidationError("MICROSOFT_CLIENT_ID must be a valid UUID.")

    domain = extract_https_domain(resolved_site_url, require_https=require_https)

    raw_template = MANIFEST_TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = (
        raw_template.replace("${{MICROSOFT_TEAMS_APP_ID}}", resolved_app_id)
        .replace("${{MICROSOFT_CLIENT_ID}}", resolved_client_id)
        .replace("${{SITE_DOMAIN}}", domain)
    )
    manifest = json.loads(rendered)
    validate_teams_manifest(manifest, require_https=require_https)
    return manifest


def validate_teams_manifest(manifest: dict[str, Any], *, require_https: bool = True) -> None:
    """Validate structural, security, and SSO invariants of a Teams app manifest."""
    if not isinstance(manifest, dict):
        raise ValidationError("Teams manifest must be a JSON object.")

    for required_key in (
        "manifestVersion",
        "version",
        "id",
        "packageName",
        "developer",
        "name",
        "description",
        "staticTabs",
        "validDomains",
        "webApplicationInfo",
    ):
        if required_key not in manifest:
            raise ValidationError(f"Teams manifest is missing required field '{required_key}'.")

    if not _is_valid_uuid(manifest.get("id", "")):
        raise ValidationError("Teams manifest 'id' must be a valid UUID.")

    web_app_info = manifest.get("webApplicationInfo") or {}
    if not _is_valid_uuid(web_app_info.get("id", "")):
        raise ValidationError("Teams manifest 'webApplicationInfo.id' must be a valid UUID.")
    if not str(web_app_info.get("resource") or "").startswith("api://"):
        raise ValidationError(
            "Teams manifest 'webApplicationInfo.resource' must start with 'api://'."
        )

    tabs = manifest.get("staticTabs") or []
    if not isinstance(tabs, list) or not tabs:
        raise ValidationError("Teams manifest must define at least one staticTab.")
    for tab in tabs:
        for url_field in ("contentUrl", "websiteUrl"):
            url_val = str(tab.get(url_field) or "")
            parsed = urlparse(url_val)
            if require_https and parsed.scheme.lower() != "https":
                raise ValidationError(
                    f"Teams tab '{tab.get('entityId')}' {url_field} must use https://."
                )

    valid_domains = manifest.get("validDomains") or []
    if not isinstance(valid_domains, list) or not valid_domains:
        raise ValidationError("Teams manifest 'validDomains' must be a non-empty list.")
    for domain in valid_domains:
        if "${{" in str(domain) or "://" in str(domain):
            raise ValidationError(
                f"Invalid domain '{domain}' in Teams manifest validDomains."
            )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def generate_branded_png(
    width: int,
    height: int,
    *,
    bg_rgba: tuple[int, int, int, int] = (15, 76, 129, 255),
    fg_rgba: tuple[int, int, int, int] = (255, 255, 255, 255),
) -> bytes:
    """Generate a valid RGBA PNG icon of exact dimensions (192x192 color or 32x32 outline)."""
    raw_rows = bytearray()
    border = max(2, width // 16)
    inner_min_x = width // 4
    inner_max_x = (width * 3) // 4
    inner_min_y = height // 4
    inner_max_y = (height * 3) // 4
    bar_thickness = max(2, height // 10)
    mid_y = height // 2

    for y in range(height):
        raw_rows.append(0)  # PNG filter type 0 (None)
        for x in range(width):
            is_border = (
                x < border
                or x >= (width - border)
                or y < border
                or y >= (height - border)
            )
            # Stylized "F" glyph in the center
            is_f_glyph = (
                inner_min_x <= x <= inner_max_x
                and inner_min_y <= y <= inner_max_y
                and (
                    x < (inner_min_x + bar_thickness)
                    or y < (inner_min_y + bar_thickness)
                    or (
                        mid_y - bar_thickness // 2
                        <= y
                        <= mid_y + bar_thickness // 2
                        and x <= (inner_max_x - bar_thickness)
                    )
                )
            )
            pixel = fg_rgba if (is_border or is_f_glyph) else bg_rgba
            raw_rows.extend(pixel)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_rows), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def ensure_teams_icons() -> tuple[bytes, bytes]:
    """Ensure 192x192 color.png and 32x32 outline.png exist in teams/ and return their bytes."""
    TEAMS_DIR.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_COLOR_ICON_PATH.exists():
        DEFAULT_COLOR_ICON_PATH.write_bytes(
            generate_branded_png(
                192, 192, bg_rgba=(15, 76, 129, 255), fg_rgba=(255, 255, 255, 255)
            )
        )
    if not DEFAULT_OUTLINE_ICON_PATH.exists():
        DEFAULT_OUTLINE_ICON_PATH.write_bytes(
            generate_branded_png(
                32, 32, bg_rgba=(15, 76, 129, 0), fg_rgba=(255, 255, 255, 255)
            )
        )
    return DEFAULT_COLOR_ICON_PATH.read_bytes(), DEFAULT_OUTLINE_ICON_PATH.read_bytes()


def package_teams_app_zip(
    output_path: str | Path,
    *,
    app_id: str | None = None,
    client_id: str | None = None,
    site_url: str | None = None,
    require_https: bool = True,
) -> Path:
    """Build and validate a ready-to-upload Microsoft Teams app .zip archive."""
    manifest = build_teams_manifest(
        app_id=app_id,
        client_id=client_id,
        site_url=site_url,
        require_https=require_https,
    )
    color_bytes, outline_bytes = ensure_teams_icons()
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("color.png", color_bytes)
        zf.writestr("outline.png", outline_bytes)

    return target
