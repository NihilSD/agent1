"""Renders a filled-in template schema + product images into a PDF, using an
HTML/CSS Jinja2 template rendered through WeasyPrint (chosen over
reportlab/fpdf2 for layout fidelity -- CSS makes it much easier to reproduce
the header/section/table styling extracted from the original template).
"""
from __future__ import annotations

import base64
import os
import uuid

import aiohttp
from jinja2 import Environment, FileSystemLoader, select_autoescape

import config

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

_jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)


class PDFGenerationError(Exception):
    pass


async def _download_image_as_data_uri(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            if "image" not in content_type:
                content_type = "image/jpeg"
            data = await resp.read()
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
    except Exception:
        return None


async def _resolve_image_data_uris(image_urls: list[str]) -> list[str]:
    if not image_urls:
        return []
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        results = []
        for url in image_urls[: config.SCRAPE_MAX_IMAGES]:
            data_uri = await _download_image_as_data_uri(session, url)
            if data_uri:
                results.append(data_uri)
        return results


async def generate_spec_sheet_pdf(
    filled_data: dict,
    style: dict,
    image_urls: list[str],
    source_url: str,
    price: str = "N/A",
) -> str:
    """Returns the path to a generated PDF file in config.TEMP_DIR."""
    try:
        import weasyprint
    except ImportError as e:
        raise PDFGenerationError(
            "weasyprint is not installed (or its system libraries like pango/cairo are missing)"
        ) from e

    image_data_uris = await _resolve_image_data_uris(image_urls)

    logo_data_uri = None
    logo_b64 = style.get("logo_base64")
    if logo_b64:
        logo_data_uri = f"data:image/png;base64,{logo_b64}"

    template = _jinja_env.get_template("spec_sheet.html")
    html_content = template.render(
        document_title=filled_data.get("document_title", "Product Specification Sheet"),
        sections=filled_data.get("sections", []),
        primary_color=style.get("primary_color", "#1F2937"),
        heading_font=style.get("heading_font", "Helvetica"),
        body_font=style.get("body_font", "Helvetica"),
        logo_data_uri=logo_data_uri,
        image_paths=image_data_uris,
        price=price,
        source_url=source_url,
    )

    output_path = os.path.join(config.TEMP_DIR, f"spec_sheet_{uuid.uuid4().hex}.pdf")
    try:
        weasyprint.HTML(string=html_content, base_url=TEMPLATES_DIR).write_pdf(output_path)
    except Exception as e:
        raise PDFGenerationError(f"Failed to render PDF: {e}") from e

    return output_path
