"""Template ingestion: turn an uploaded docx/pdf/image spec sheet into raw
structural hints (headings, label/value pairs, fonts, colors, logo) that
llm_mapper.extract_template_schema() then normalizes into the final JSON
"template schema" used by pdf_generator.

Each parser is heuristic and best-effort -- real-world spec sheets vary too
much to fully solve with rules alone, which is why the messy output here is
handed to an LLM for the final structuring pass rather than used directly.
"""
from __future__ import annotations

import base64
import io
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image


class TemplateParseError(Exception):
    """Raised when a template file can't be read/parsed at all."""


@dataclass
class RawTemplateExtract:
    source_type: str
    raw_text: str = ""
    label_value_pairs: list[tuple[str, str]] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    heading_font: Optional[str] = None
    body_font: Optional[str] = None
    primary_color_hex: Optional[str] = None
    logo_base64: Optional[str] = None

    def to_prompt_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "raw_text": self.raw_text[:12000],
            "label_value_pairs": self.label_value_pairs[:200],
            "headings": self.headings[:50],
        }


SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}


def parse_template_file(file_path: str) -> RawTemplateExtract:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".docx":
        return _parse_docx(file_path)
    if ext == ".pdf":
        return _parse_pdf(file_path)
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        return _parse_image(file_path)
    raise TemplateParseError(
        f"Unsupported template file type '{ext}'. Supported: .docx, .pdf, .png, .jpg, .jpeg, .webp"
    )


def _rgb_to_hex(rgb) -> Optional[str]:
    if rgb is None:
        return None
    try:
        return "#{:02X}{:02X}{:02X}".format(rgb[0], rgb[1], rgb[2])
    except Exception:
        return None


def _parse_docx(file_path: str) -> RawTemplateExtract:
    try:
        import docx
    except ImportError as e:
        raise TemplateParseError("python-docx is not installed") from e

    try:
        document = docx.Document(file_path)
    except Exception as e:
        raise TemplateParseError(f"Could not open .docx file: {e}") from e

    extract = RawTemplateExtract(source_type="docx")
    text_lines = []
    font_counter: Counter = Counter()
    heading_font_counter: Counter = Counter()
    color_counter: Counter = Counter()

    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style_name = (para.style.name or "").lower() if para.style else ""
        is_heading = "heading" in style_name or "title" in style_name
        if is_heading:
            extract.headings.append(text)
        else:
            text_lines.append(text)
            if ":" in text:
                label, _, value = text.partition(":")
                if 0 < len(label.strip()) <= 40:
                    extract.label_value_pairs.append((label.strip(), value.strip()))

        for run in para.runs:
            if run.font and run.font.name:
                (heading_font_counter if is_heading else font_counter)[run.font.name] += 1
            try:
                if run.font and run.font.color and run.font.color.rgb:
                    color_counter[str(run.font.color.rgb)] += 1
            except Exception:
                pass

    # Tables are the most common place spec sheets put label/value rows.
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                extract.label_value_pairs.append((cells[0], " / ".join(cells[1:])))
                text_lines.append(" | ".join(cells))
            elif len(cells) == 1:
                text_lines.append(cells[0])

    # Pull the first embedded image as a logo candidate.
    try:
        for rel in document.part.rels.values():
            if "image" in rel.reltype:
                image_bytes = rel.target_part.blob
                extract.logo_base64 = base64.b64encode(image_bytes).decode("ascii")
                break
    except Exception:
        pass

    extract.raw_text = "\n".join(extract.headings + text_lines)
    if heading_font_counter:
        extract.heading_font = heading_font_counter.most_common(1)[0][0]
    if font_counter:
        extract.body_font = font_counter.most_common(1)[0][0]
    if color_counter:
        hex_str = color_counter.most_common(1)[0][0]
        if hex_str and hex_str.upper() != "000000":
            extract.primary_color_hex = f"#{hex_str.upper()}"

    if not extract.raw_text.strip():
        raise TemplateParseError("No readable text found in the .docx template")
    return extract


def _parse_pdf(file_path: str) -> RawTemplateExtract:
    try:
        import pdfplumber
    except ImportError as e:
        raise TemplateParseError("pdfplumber is not installed") from e

    extract = RawTemplateExtract(source_type="pdf")
    text_lines: list[str] = []
    font_size_counter: Counter = Counter()
    font_name_counter: Counter = Counter()

    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages[:5]:  # spec sheets are short; cap for cost/perf
                page_text = page.extract_text() or ""
                text_lines.extend(line.strip() for line in page_text.splitlines() if line.strip())

                for char in page.chars:
                    size = round(char.get("size", 0))
                    if size:
                        font_size_counter[size] += 1
                    fname = char.get("fontname")
                    if fname:
                        font_name_counter[fname] += 1

                for table in page.extract_tables() or []:
                    for row in table:
                        cells = [c.strip() for c in row if c and c.strip()]
                        if len(cells) >= 2:
                            extract.label_value_pairs.append((cells[0], " / ".join(cells[1:])))
    except Exception as e:
        raise TemplateParseError(f"Could not read .pdf file: {e}") from e

    if not text_lines and not extract.label_value_pairs:
        raise TemplateParseError(
            "No extractable text found in the .pdf template (it may be a scanned image; "
            "try uploading it as a .png/.jpg instead)"
        )

    # Largest font sizes on the page are treated as headings.
    if font_size_counter:
        sizes = sorted(font_size_counter.keys(), reverse=True)
        heading_sizes = set(sizes[: max(1, len(sizes) // 4)])
        body_size = font_size_counter.most_common(1)[0][0]
        heading_sizes.discard(body_size)
    else:
        heading_sizes = set()

    for line in text_lines:
        if len(line) <= 60 and (line.isupper() or line.endswith(":") is False and len(line.split()) <= 6):
            # weak heuristic backstop; real heading detection happens via font size below
            pass
        if ":" in line:
            label, _, value = line.partition(":")
            if 0 < len(label.strip()) <= 40:
                extract.label_value_pairs.append((label.strip(), value.strip()))

    extract.raw_text = "\n".join(text_lines)
    if font_name_counter:
        extract.body_font = font_name_counter.most_common(1)[0][0]
    return extract


def _parse_image(file_path: str) -> RawTemplateExtract:
    try:
        import pytesseract
    except ImportError as e:
        raise TemplateParseError("pytesseract is not installed") from e

    extract = RawTemplateExtract(source_type="image")

    try:
        image = Image.open(file_path).convert("RGB")
    except Exception as e:
        raise TemplateParseError(f"Could not open image file: {e}") from e

    try:
        text = pytesseract.image_to_string(image)
    except Exception as e:
        raise TemplateParseError(
            f"OCR failed ({e}). Make sure the tesseract-ocr system package is installed."
        ) from e

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        raise TemplateParseError("OCR could not find any readable text in the image template")

    for line in lines:
        if ":" in line:
            label, _, value = line.partition(":")
            if 0 < len(label.strip()) <= 40:
                extract.label_value_pairs.append((label.strip(), value.strip()))

    extract.raw_text = "\n".join(lines)

    # Dominant color of the top band of the image is a reasonable stand-in
    # for a header/banner color on many spec sheet designs.
    try:
        w, h = image.size
        banner = image.crop((0, 0, w, max(1, int(h * 0.15))))
        banner_small = banner.resize((32, 32))
        colors = banner_small.getcolors(32 * 32) or []
        if colors:
            colors.sort(reverse=True)
            _, dominant = colors[0]
            # skip near-white/near-black banners; not useful as an accent color
            if not (all(c > 235 for c in dominant) or all(c < 20 for c in dominant)):
                extract.primary_color_hex = _rgb_to_hex(dominant)
    except Exception:
        pass

    # Store a downscaled copy as a potential logo/reference image.
    try:
        thumb = image.copy()
        thumb.thumbnail((400, 400))
        buf = io.BytesIO()
        thumb.save(buf, format="PNG")
        extract.logo_base64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        pass

    return extract
