"""AliExpress product listing scraper, built on Playwright (headless Chromium)
since the listing page is JS-rendered and actively defends against plain
`requests`-style scraping.

AliExpress frequently changes its markup/anti-bot challenges, so this module
uses a layered strategy and degrades gracefully instead of throwing on the
first mismatch:

  1. Look for the `window.runParams = {...}` (or similar) JSON blob that
     AliExpress historically embeds server-side with the full product model.
  2. Fall back to JSON-LD (`<script type="application/ld+json">`).
  3. Fall back to plain DOM scraping of visible elements.

If the page is blocked/rate-limited (captcha, redirect to a login/verify
page, empty content) a `ScrapeError` is raised with a message that's safe to
show directly to a Discord user.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional

import config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = [
    "punish",
    "captcha",
    "verify you are a human",
    "access denied",
    "slider verification",
]


class ScrapeError(Exception):
    """Raised for anything that should surface as a friendly Discord message."""


@dataclass
class ProductData:
    title: str = ""
    images: list[str] = field(default_factory=list)
    price: str = "N/A"
    specs: dict[str, str] = field(default_factory=dict)
    description: str = ""
    variants: list[str] = field(default_factory=list)
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "images": self.images,
            "price": self.price,
            "specs": self.specs,
            "description": self.description,
            "variants": self.variants,
            "url": self.url,
        }


def _looks_blocked(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def _extract_run_params(html: str) -> Optional[dict]:
    """AliExpress product pages have historically embedded the product model
    as `window.runParams = {...};` in an inline <script>. Not guaranteed to
    be present on every template/locale, hence the fallbacks elsewhere."""
    match = re.search(r"window\.runParams\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not match:
        match = re.search(r"runParams\s*=\s*(\{.*?\})\s*</script>", html, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _extract_json_ld(html: str) -> list[dict]:
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    results = []
    for block in blocks:
        try:
            data = json.loads(block.strip())
            results.extend(data if isinstance(data, list) else [data])
        except json.JSONDecodeError:
            continue
    return results


def _from_run_params(run_params: dict, product: ProductData) -> None:
    try:
        page_module = run_params.get("data", {}).get("pageModule", {})
        if page_module.get("title"):
            product.title = page_module["title"]
        imgs = page_module.get("imagePathList") or page_module.get("imageModule", {}).get("imagePathList")
        if imgs:
            product.images = imgs[: config.SCRAPE_MAX_IMAGES]
    except Exception:
        pass

    try:
        price_module = run_params.get("data", {}).get("priceModule", {})
        price = (
            price_module.get("formatedActivityPrice")
            or price_module.get("formatedPrice")
            or price_module.get("minActivityAmount", {}).get("formatedAmount")
        )
        if price:
            product.price = price
    except Exception:
        pass

    try:
        spec_module = run_params.get("data", {}).get("specsModule", {})
        for prop in spec_module.get("props", []):
            name = prop.get("attrName")
            value = prop.get("attrValue") or prop.get("value")
            if name and value:
                product.specs[name] = value
    except Exception:
        pass

    try:
        desc_module = run_params.get("data", {}).get("descriptionModule", {})
        if desc_module.get("description"):
            product.description = desc_module["description"]
    except Exception:
        pass

    try:
        sku_module = run_params.get("data", {}).get("skuModule", {})
        for prop in sku_module.get("productSKUPropertyList", []):
            values = [v.get("propertyValueDisplayName", "") for v in prop.get("skuPropertyValues", [])]
            values = [v for v in values if v]
            if values:
                product.variants.append(f"{prop.get('skuPropertyName', 'Option')}: {', '.join(values)}")
    except Exception:
        pass


def _from_json_ld(ld_blocks: list[dict], product: ProductData) -> None:
    for block in ld_blocks:
        if block.get("@type") not in ("Product", "product"):
            continue
        if not product.title and block.get("name"):
            product.title = block["name"]
        image = block.get("image")
        if image and not product.images:
            product.images = (image if isinstance(image, list) else [image])[: config.SCRAPE_MAX_IMAGES]
        offers = block.get("offers")
        if offers and product.price == "N/A":
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price")
            currency = offers.get("priceCurrency", "")
            if price:
                product.price = f"{currency} {price}".strip()
        if block.get("description") and not product.description:
            product.description = block["description"]


async def _scrape_with_dom(page, product: ProductData) -> None:
    """Last-resort visible-DOM scraping when structured data isn't found."""
    if not product.title:
        for selector in ["h1", "[class*='title']"]:
            el = await page.query_selector(selector)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    product.title = text
                    break

    if not product.images:
        image_urls = await page.eval_on_selector_all(
            "img[src*='alicdn']",
            "els => els.map(e => e.src)",
        )
        seen = []
        for url in image_urls:
            if url not in seen and "logo" not in url.lower():
                seen.append(url)
            if len(seen) >= config.SCRAPE_MAX_IMAGES:
                break
        product.images = seen

    if product.price == "N/A":
        for selector in ["[class*='price']", "[class*='Price']"]:
            el = await page.query_selector(selector)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    product.price = text
                    break

    if not product.specs:
        rows = await page.query_selector_all("[class*='spec'] li, [class*='Specs'] li, table tr")
        for row in rows[:60]:
            text = (await row.inner_text()).strip().replace("\n", " ")
            if ":" in text:
                label, _, value = text.partition(":")
            elif "\t" in text:
                label, _, value = text.partition("\t")
            else:
                parts = re.split(r"\s{2,}", text, maxsplit=1)
                if len(parts) != 2:
                    continue
                label, value = parts
            label, value = label.strip(), value.strip()
            if label and value and len(label) <= 40:
                product.specs[label] = value

    if not product.description:
        el = await page.query_selector("[class*='description'], #product-description")
        if el:
            product.description = (await el.inner_text()).strip()[:4000]


async def scrape_listing(url: str) -> ProductData:
    if "aliexpress." not in url:
        raise ScrapeError("That doesn't look like an AliExpress product URL.")

    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise ScrapeError("Playwright is not installed on the bot host.") from e

    product = ProductData(url=url)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = await context.new_page()
            try:
                await page.goto(url, timeout=config.SCRAPE_TIMEOUT_MS, wait_until="domcontentloaded")
                # small human-like pause before reading the DOM
                await asyncio.sleep(2)
                try:
                    await page.wait_for_selector("h1", timeout=8000)
                except Exception:
                    pass

                html = await page.content()
                if _looks_blocked(html):
                    raise ScrapeError(
                        "AliExpress blocked this request (captcha/rate limit). "
                        "Please try again in a few minutes, or try a different listing."
                    )

                run_params = _extract_run_params(html)
                if run_params:
                    _from_run_params(run_params, product)

                if not product.title or not product.images or product.price == "N/A":
                    ld_blocks = _extract_json_ld(html)
                    if ld_blocks:
                        _from_json_ld(ld_blocks, product)

                if not product.title or not product.images:
                    await _scrape_with_dom(page, product)
            finally:
                await context.close()
                await browser.close()
    except ScrapeError:
        raise
    except Exception as e:
        raise ScrapeError(f"Failed to load or parse the AliExpress listing: {e}") from e

    if not product.title:
        raise ScrapeError(
            "Couldn't extract product details from that listing. The page layout may have "
            "changed or the listing may be unavailable."
        )

    return product
