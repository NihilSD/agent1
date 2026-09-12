"""Command-line AI-powered spec sheet generator.

Usage:
    python main.py set-template <path/to/template.docx|pdf|png|jpg>
    python main.py generate <aliexpress_url> [-o output.pdf]
    python main.py current-template
    python main.py reset-template
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import config
import llm_mapper
import pdf_generator
import scraper
import storage
import template_parser


def _fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def cmd_set_template(args: argparse.Namespace) -> None:
    path = args.file
    if not os.path.isfile(path):
        _fail(f"File not found: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext not in template_parser.SUPPORTED_EXTENSIONS:
        _fail(
            f"Unsupported file type '{ext}'. Supported: "
            f"{', '.join(sorted(template_parser.SUPPORTED_EXTENSIONS))}"
        )

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > config.MAX_UPLOAD_MB:
        _fail(f"That file is {size_mb:.1f}MB, which is over the {config.MAX_UPLOAD_MB}MB limit.")

    print(f"Parsing template: {path}")
    try:
        raw_extract = template_parser.parse_template_file(path)
    except template_parser.TemplateParseError as e:
        _fail(f"Couldn't parse that template: {e}")

    print("Analyzing structure with the LLM...")
    try:
        schema = llm_mapper.extract_template_schema(raw_extract)
    except llm_mapper.LLMError as e:
        _fail(f"Couldn't determine the template's structure: {e}")

    storage.save_template(schema, os.path.basename(path))

    print(f"\nTemplate saved from '{os.path.basename(path)}'.\n")
    _print_schema_summary(schema)
    print("\nRun 'python main.py generate <aliexpress_url>' to build a spec sheet from it.")


def cmd_generate(args: argparse.Namespace) -> None:
    stored = storage.get_template()
    if not stored:
        _fail("No template is set yet. Run 'python main.py set-template <file>' first.")
    schema = stored["schema"]

    print(f"Scraping listing: {args.url}")
    try:
        product = asyncio.run(scraper.scrape_listing(args.url))
    except scraper.ScrapeError as e:
        _fail(f"Couldn't get that listing: {e}")

    print(f"Found product: {product.title}")

    print("Mapping product data onto the template...")
    try:
        filled = llm_mapper.map_product_to_template(schema, product.to_dict())
    except llm_mapper.LLMError as e:
        _fail(f"Couldn't map the product data onto the template: {e}")

    print("Rendering PDF...")
    try:
        pdf_path = asyncio.run(
            pdf_generator.generate_spec_sheet_pdf(
                filled_data=filled,
                style=schema.get("style", {}),
                image_urls=product.images,
                source_url=args.url,
                price=product.price,
            )
        )
    except pdf_generator.PDFGenerationError as e:
        _fail(f"Couldn't generate the PDF: {e}")

    output_path = args.output or _default_output_path(product.title)
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    os.replace(pdf_path, output_path)

    print(f"\nSpec sheet saved to: {output_path}")


def cmd_current_template(args: argparse.Namespace) -> None:
    stored = storage.get_template()
    if not stored:
        print("No template is set yet. Use 'set-template' to upload one.")
        return
    print(f"Active template (from '{stored['source_filename']}'):\n")
    _print_schema_summary(stored["schema"])


def cmd_reset_template(args: argparse.Namespace) -> None:
    if storage.delete_template():
        print("Template cleared.")
    else:
        print("There was no template set.")


def _print_schema_summary(schema: dict) -> None:
    print(schema.get("document_title", "Spec Sheet"))
    for section in schema["sections"]:
        print(f"  - {section['name']}: {', '.join(section['fields'])}")


def _default_output_path(product_title: str) -> str:
    safe = "".join(c for c in product_title if c.isalnum() or c in (" ", "-", "_")).strip()
    safe = safe.replace(" ", "_")[:60] or "spec_sheet"
    return os.path.join("output", f"{safe}.pdf")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-powered spec sheet generator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_set = subparsers.add_parser("set-template", help="Set the reference spec sheet template (docx/pdf/image)")
    p_set.add_argument("file", help="Path to the template file")
    p_set.set_defaults(func=cmd_set_template)

    p_gen = subparsers.add_parser("generate", help="Generate a spec sheet PDF from an AliExpress listing")
    p_gen.add_argument("url", help="AliExpress product listing URL")
    p_gen.add_argument("-o", "--output", help="Output PDF path (default: output/<product-name>.pdf)")
    p_gen.set_defaults(func=cmd_generate)

    p_current = subparsers.add_parser("current-template", help="Show the active template")
    p_current.set_defaults(func=cmd_current_template)

    p_reset = subparsers.add_parser("reset-template", help="Clear the active template")
    p_reset.set_defaults(func=cmd_reset_template)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
