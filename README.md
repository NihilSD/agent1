# AI-Powered Spec Sheet Generator (Discord Bot)

A Discord bot that turns a reference spec sheet template (docx/pdf/image) into a
reusable schema, then generates new, filled-in spec sheet PDFs for any AliExpress
product listing you give it.

## How it works

1. `/set_template` — upload a reference spec sheet once. The bot parses it
   (`template_parser.py`) and uses an LLM (`llm_mapper.py`) to turn it into a
   structured JSON "template schema" (sections, field labels, fonts/colors),
   which is stored per Discord server (`storage.py`, SQLite).
2. `/generate <aliexpress_url>` — the bot scrapes the listing with a headless
   browser (`scraper.py`, Playwright), asks the LLM to map the scraped data onto
   the stored template's field labels (`llm_mapper.py`), renders the result as a
   styled PDF (`pdf_generator.py`, Jinja2 + WeasyPrint), and replies with the file.
3. Re-run `/generate` with as many different listing URLs as you like against the
   same stored template.
4. `/current_template` shows what's active; `/reset_template` clears it so you can
   upload a new one.

## Project structure

```
bot.py               Discord bot / slash commands
scraper.py            AliExpress listing scraper (Playwright)
template_parser.py    docx/pdf/image template ingestion -> raw structural hints
llm_mapper.py         LLM calls: template schema extraction + field mapping
pdf_generator.py       HTML/CSS (Jinja2) -> PDF via WeasyPrint
storage.py             SQLite persistence of template schemas per server/user
config.py              Environment configuration
templates/spec_sheet.html   HTML/CSS layout used to render generated PDFs
```

## Setup

### 1. Discord bot

1. Create an application + bot at https://discord.com/developers/applications.
2. Under **Bot**, enable the bot and copy its token.
3. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`,
   and permissions `Send Messages` + `Attach Files`. Use the generated URL to invite
   the bot to your server.
4. No privileged gateway intents are required (the bot only uses slash commands).

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. System dependencies

- **WeasyPrint** needs Pango/Cairo/GDK-Pixbuf system libraries. On Debian/Ubuntu:
  ```bash
  sudo apt-get install -y libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf2.0-0 libffi-dev
  ```
  See https://doc.courtbouillon.org/weasyprint/stable/first_steps.html for other OSes.
- **pytesseract** (used to OCR image-based templates) needs the `tesseract-ocr` binary:
  ```bash
  sudo apt-get install -y tesseract-ocr
  ```

### 4. Configuration

```bash
cp .env.example .env
```

Fill in `.env`:
- `DISCORD_TOKEN` — your bot token.
- `LLM_PROVIDER` — `anthropic` (default) or `openai`.
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — whichever provider you chose.

### 5. Run

```bash
python bot.py
```

On startup the bot syncs its slash commands; they may take a few minutes to
appear globally the first time (per-guild sync via `on_ready` is immediate for
new guilds the bot restarts into).

## Usage

```
/set_template attachment:<garden-lamp-spec-sheet.docx>
/generate aliexpress_url:https://www.aliexpress.com/item/1005006123456789.html
/generate aliexpress_url:https://www.aliexpress.com/item/1005006987654321.html
/current_template
/reset_template
```

## Notes & limitations

- **AliExpress scraping**: the scraper looks for AliExpress's embedded
  `window.runParams` product JSON first, falls back to JSON-LD, then to plain DOM
  scraping. AliExpress changes its markup and anti-bot challenges often — if scraping
  starts failing, `scraper.py` is the place to update selectors. Blocked/rate-limited
  pages are detected and reported back to the user instead of failing silently or
  returning garbage data.
- **Field mapping never guesses**: the LLM is explicitly instructed to output `"N/A"`
  for any field it can't confidently determine from the scraped data, rather than
  fabricating plausible-looking values.
- **Template fidelity**: rather than pixel-reproducing the original file's exact
  layout, the bot extracts its *structure* (sections, field order/labels, heading/body
  fonts, an accent color, and an embedded logo/reference image if present) and
  re-renders that structure through a clean HTML/CSS layout
  (`templates/spec_sheet.html`). This keeps generated PDFs consistent and readable
  across wildly different source formats (docx/pdf/scanned image).
- **Image-based templates** are parsed with OCR (`pytesseract`); scanned/rasterized
  PDFs won't have extractable text either — re-upload the page as a `.png`/`.jpg`.
- **Storage scope**: templates are stored per Discord server (`guild_id`). In a DM,
  they fall back to per-user storage.
