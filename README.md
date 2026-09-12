# AI-Powered Spec Sheet Generator

A command-line tool that turns a reference spec sheet template (docx/pdf/image) into a
reusable schema, then generates new, filled-in spec sheet PDFs for any AliExpress
product listing you give it. Runs entirely on your own machine.

## How it works

1. `set-template` — point it at a reference spec sheet once. The tool parses it
   (`template_parser.py`) and uses an LLM (`llm_mapper.py`) to turn it into a
   structured JSON "template schema" (sections, field labels, fonts/colors),
   which is stored locally (`storage.py`, SQLite).
2. `generate <aliexpress_url>` — it scrapes the listing with a headless browser
   (`scraper.py`, Playwright), asks the LLM to map the scraped data onto the
   stored template's field labels (`llm_mapper.py`), renders the result as a
   styled PDF (`pdf_generator.py`, Jinja2 + WeasyPrint), and saves it to disk.
3. Re-run `generate` with as many different listing URLs as you like against the
   same stored template.
4. `current-template` shows what's active; `reset-template` clears it so you can
   set a new one.

## Project structure

```
main.py                CLI entry point (set-template / generate / current-template / reset-template)
scraper.py              AliExpress listing scraper (Playwright)
template_parser.py      docx/pdf/image template ingestion -> raw structural hints
llm_mapper.py           LLM calls: template schema extraction + field mapping
pdf_generator.py         HTML/CSS (Jinja2) -> PDF via WeasyPrint
storage.py               SQLite persistence of the active template schema
config.py                Environment configuration
templates/spec_sheet.html   HTML/CSS layout used to render generated PDFs
```

## Setup

### 1. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. System dependencies

- **WeasyPrint** needs Pango/Cairo/GDK-Pixbuf system libraries.
  - Debian/Ubuntu: `sudo apt-get install -y libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf2.0-0 libffi-dev`
  - macOS: `brew install pango cairo gdk-pixbuf libffi`
  - Windows: see https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows
- **pytesseract** (used to OCR image-based templates) needs the `tesseract-ocr` binary:
  - Debian/Ubuntu: `sudo apt-get install -y tesseract-ocr`
  - macOS: `brew install tesseract`
  - Windows: install from https://github.com/UB-Mannheim/tesseract/wiki

### 3. Configuration

```bash
cp .env.example .env
```

Fill in `.env`:
- `LLM_PROVIDER` — `anthropic` (default) or `openai`.
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — whichever provider you chose.

## Usage

```bash
# One-time: teach the tool your reference spec sheet's layout
python main.py set-template ./garden-lamp-spec-sheet.docx

# Generate a filled spec sheet for a product listing
python main.py generate https://www.aliexpress.com/item/1005006123456789.html
# -> saved to output/<product-name>.pdf

# Run it again with a different listing, same stored template
python main.py generate https://www.aliexpress.com/item/1005006987654321.html -o my_sheet.pdf

# Inspect / clear the stored template
python main.py current-template
python main.py reset-template
```

## Notes & limitations

- **AliExpress scraping**: the scraper looks for AliExpress's embedded
  `window.runParams` product JSON first, falls back to JSON-LD, then to plain DOM
  scraping. AliExpress changes its markup and anti-bot challenges often — if scraping
  starts failing, `scraper.py` is the place to update selectors. Blocked/rate-limited
  pages are detected and reported with a clear error instead of failing silently or
  returning garbage data.
- **Field mapping never guesses**: the LLM is explicitly instructed to output `"N/A"`
  for any field it can't confidently determine from the scraped data, rather than
  fabricating plausible-looking values.
- **Template fidelity**: rather than pixel-reproducing the original file's exact
  layout, the tool extracts its *structure* (sections, field order/labels, heading/body
  fonts, an accent color, and an embedded logo/reference image if present) and
  re-renders that structure through a clean HTML/CSS layout
  (`templates/spec_sheet.html`). This keeps generated PDFs consistent and readable
  across wildly different source formats (docx/pdf/scanned image).
- **Image-based templates** are parsed with OCR (`pytesseract`); scanned/rasterized
  PDFs won't have extractable text either — re-save/export that page as a `.png`/`.jpg`
  and use that instead.
- **Storage**: the active template schema is stored in a local SQLite database
  (`data/templates.db`) — one active template at a time, replaced whenever you run
  `set-template` again.
