"""LLM-backed steps:

1. `extract_template_schema` - turn the messy heuristic output of
   template_parser.py into a clean, structured "template schema" (sections +
   field labels + tone notes). Style facts that were reliably extracted by
   heuristics (fonts/colors/logo) are merged in afterwards, not re-derived by
   the LLM.
2. `map_product_to_template` - align the scraped AliExpress product data onto
   that schema's field labels (names rarely match exactly), inferring
   reasonable values from the free-text description where sensible, and
   filling anything it can't confidently determine with "N/A".

Supports either Anthropic or OpenAI as the backing model via LLM_PROVIDER.
"""
from __future__ import annotations

import json
import re
from typing import Any

import config


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict:
    """Models occasionally wrap JSON in prose or code fences; salvage it."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model did not return valid JSON: {e}") from e


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    if config.LLM_PROVIDER == "anthropic":
        return _call_anthropic(system_prompt, user_prompt)
    if config.LLM_PROVIDER == "openai":
        return _call_openai(system_prompt, user_prompt)
    raise LLMError(f"Unsupported LLM_PROVIDER '{config.LLM_PROVIDER}' (use 'anthropic' or 'openai')")


def _call_anthropic(system_prompt: str, user_prompt: str) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("anthropic package is not installed") from e
    if not config.ANTHROPIC_API_KEY:
        raise LLMError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def _call_openai(system_prompt: str, user_prompt: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise LLMError("openai package is not installed") from e
    if not config.OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content or ""


TEMPLATE_SCHEMA_SYSTEM_PROMPT = """You are a document-structure analyst. You will be given raw, imperfectly \
extracted content from a product spec sheet (a docx/pdf/image template). Your job is to infer the \
document's reusable STRUCTURE so it can be reused as a blank template for other products.

Output STRICT JSON only, matching this shape exactly:
{
  "document_title": "<generic title pattern, e.g. 'Product Specification Sheet'>",
  "sections": [
    {"name": "<section heading>", "fields": ["<field label>", "<field label>", ...]}
  ],
  "tone_notes": "<1-2 sentences describing writing style/tone to preserve, e.g. 'concise, technical bullet points'>"
}

Rules:
- Group field labels (e.g. "Material", "Wattage", "Dimensions", "IP Rating") under sensible section \
headings inferred from the source (e.g. "Technical Specifications", "Package Contents"). If the source \
has no clear sections, create one section named "Specifications" containing all fields.
- Preserve the field labels' original wording and order as much as possible.
- Do not invent fields that have no evidence in the source text.
- Do not include actual product values, only the structural labels.
- Output nothing but the JSON object."""


def extract_template_schema(raw_extract) -> dict:
    """raw_extract: template_parser.RawTemplateExtract"""
    prompt_data = raw_extract.to_prompt_dict()
    user_prompt = (
        "Here is the raw extracted content from the uploaded template:\n\n"
        f"{json.dumps(prompt_data, ensure_ascii=False, indent=2)}\n\n"
        "Produce the structural template schema JSON described in the system prompt."
    )
    raw_response = _call_llm(TEMPLATE_SCHEMA_SYSTEM_PROMPT, user_prompt)
    schema = _extract_json(raw_response)

    if "sections" not in schema or not isinstance(schema["sections"], list) or not schema["sections"]:
        raise LLMError("Model returned a template schema with no usable sections")

    schema["style"] = {
        "primary_color": raw_extract.primary_color_hex or "#1F2937",
        "heading_font": raw_extract.heading_font or "Helvetica",
        "body_font": raw_extract.body_font or "Helvetica",
        "logo_base64": raw_extract.logo_base64,
    }
    schema["source_type"] = raw_extract.source_type
    return schema


FIELD_MAPPING_SYSTEM_PROMPT = """You are a meticulous product-data assistant. You will be given (1) a \
template schema describing sections and field labels for a spec sheet, and (2) scraped data for a new \
product (title, price, a specs dict, a free-text description, variant options). Field names in the \
scraped specs will often NOT match the template's field labels exactly (e.g. "Power(W)" vs "Wattage", \
"Material" vs "Housing Material").

Your job: for every field label in every section of the template schema, decide the best matching value:
- If a scraped spec key clearly corresponds (semantically, even if worded differently), use its value.
- If no spec matches but the free-text description clearly states the value, infer it from there.
- If you cannot confidently determine a value from the given data, use exactly "N/A". Never guess or \
fabricate a plausible-sounding value that isn't actually supported by the input data.
- Preserve the template's tone/style described in "tone_notes" when phrasing values (e.g. keep it \
concise/technical), but do not alter factual content.
- Also produce a "document_title" for this specific product (e.g. "<Product Name> Specification Sheet").

Output STRICT JSON only, matching this shape exactly:
{
  "document_title": "<title for this product's sheet>",
  "sections": [
    {"name": "<section name from the template schema>", "fields": {"<field label>": "<value or 'N/A'>", ...}}
  ]
}

Include every section and every field label from the input template schema, in the same order. \
Output nothing but the JSON object."""


def map_product_to_template(template_schema: dict, product_data: dict) -> dict:
    user_prompt = (
        "TEMPLATE SCHEMA:\n"
        f"{json.dumps({'document_title': template_schema.get('document_title'), 'sections': template_schema['sections'], 'tone_notes': template_schema.get('tone_notes', '')}, ensure_ascii=False, indent=2)}\n\n"
        "SCRAPED PRODUCT DATA:\n"
        f"{json.dumps(product_data, ensure_ascii=False, indent=2)[:8000]}\n\n"
        "Produce the filled-in JSON described in the system prompt."
    )
    raw_response = _call_llm(FIELD_MAPPING_SYSTEM_PROMPT, user_prompt)
    filled = _extract_json(raw_response)

    if "sections" not in filled or not isinstance(filled["sections"], list):
        raise LLMError("Model returned filled data with no usable sections")

    # Safety net: guarantee every template field is present even if the
    # model dropped one, so the PDF layout never silently loses a row.
    filled_by_name = {s.get("name"): s.get("fields", {}) for s in filled["sections"]}
    normalized_sections = []
    for section in template_schema["sections"]:
        fields = filled_by_name.get(section["name"], {})
        normalized_fields = {label: fields.get(label, "N/A") or "N/A" for label in section["fields"]}
        normalized_sections.append({"name": section["name"], "fields": normalized_fields})

    filled["sections"] = normalized_sections
    filled.setdefault("document_title", template_schema.get("document_title", "Product Specification Sheet"))
    return filled
