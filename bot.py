"""Discord bot entrypoint.

Slash commands:
  /set_template <attachment>   - upload a reference spec sheet (docx/pdf/image)
  /generate <aliexpress_url>   - scrape a listing and generate a filled spec sheet PDF
  /current_template            - show info about the active stored template
  /reset_template               - clear the active template
"""
from __future__ import annotations

import logging
import os
import tempfile
import traceback

import discord
from discord import app_commands

import config
import llm_mapper
import pdf_generator
import scraper
import storage
import template_parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("spec_sheet_bot")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _scope_id(interaction: discord.Interaction) -> str:
    return storage.scope_id_for(interaction.guild_id, interaction.user.id)


@client.event
async def on_ready():
    try:
        synced = await tree.sync()
        log.info("Logged in as %s, synced %d command(s)", client.user, len(synced))
    except Exception:
        log.exception("Failed to sync command tree")


@tree.command(name="set_template", description="Upload a reference spec sheet (docx/pdf/image) to use as the template")
@app_commands.describe(attachment="The template file: .docx, .pdf, .png, .jpg, .jpeg, or .webp")
async def set_template(interaction: discord.Interaction, attachment: discord.Attachment):
    await interaction.response.defer(thinking=True)

    ext = os.path.splitext(attachment.filename)[1].lower()
    if ext not in template_parser.SUPPORTED_EXTENSIONS:
        await interaction.followup.send(
            f"Unsupported file type `{ext}`. Please upload one of: "
            f"{', '.join(sorted(template_parser.SUPPORTED_EXTENSIONS))}"
        )
        return

    size_mb = attachment.size / (1024 * 1024)
    if size_mb > config.MAX_UPLOAD_MB:
        await interaction.followup.send(
            f"That file is {size_mb:.1f}MB, which is over the {config.MAX_UPLOAD_MB}MB limit for templates."
        )
        return

    with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as tmp_dir:
        local_path = os.path.join(tmp_dir, attachment.filename)
        try:
            await attachment.save(local_path)
        except Exception as e:
            await interaction.followup.send(f"Couldn't download that attachment: {e}")
            return

        try:
            raw_extract = template_parser.parse_template_file(local_path)
        except template_parser.TemplateParseError as e:
            await interaction.followup.send(f"Couldn't parse that template: {e}")
            return
        except Exception:
            log.exception("Unexpected error parsing template")
            await interaction.followup.send("Something went wrong parsing that template. Please try a different file.")
            return

        try:
            schema = llm_mapper.extract_template_schema(raw_extract)
        except llm_mapper.LLMError as e:
            await interaction.followup.send(f"Couldn't determine the template's structure: {e}")
            return
        except Exception:
            log.exception("Unexpected error extracting template schema")
            await interaction.followup.send("Something went wrong analyzing that template's structure.")
            return

    storage.save_template(_scope_id(interaction), schema, attachment.filename)

    section_summary = "\n".join(
        f"- **{s['name']}**: {', '.join(s['fields'])}" for s in schema["sections"]
    )
    await interaction.followup.send(
        f"Template saved from `{attachment.filename}`.\n\n"
        f"**{schema.get('document_title', 'Spec Sheet')}**\n{section_summary}\n\n"
        f"Use `/generate <aliexpress_url>` to build a spec sheet from this template."
    )


@tree.command(name="generate", description="Generate a spec sheet PDF for an AliExpress listing using the stored template")
@app_commands.describe(aliexpress_url="The AliExpress product listing URL")
async def generate(interaction: discord.Interaction, aliexpress_url: str):
    await interaction.response.defer(thinking=True)

    stored = storage.get_template(_scope_id(interaction))
    if not stored:
        await interaction.followup.send(
            "No template is set yet for this server. Run `/set_template` first and upload a reference spec sheet."
        )
        return
    schema = stored["schema"]

    try:
        product = await scraper.scrape_listing(aliexpress_url)
    except scraper.ScrapeError as e:
        await interaction.followup.send(f"Couldn't get that listing: {e}")
        return
    except Exception:
        log.exception("Unexpected scraping error")
        await interaction.followup.send("Something went wrong scraping that listing. Please try again later.")
        return

    try:
        filled = llm_mapper.map_product_to_template(schema, product.to_dict())
    except llm_mapper.LLMError as e:
        await interaction.followup.send(f"Couldn't map the product data onto the template: {e}")
        return
    except Exception:
        log.exception("Unexpected error mapping fields")
        await interaction.followup.send("Something went wrong generating the spec sheet content.")
        return

    try:
        pdf_path = await pdf_generator.generate_spec_sheet_pdf(
            filled_data=filled,
            style=schema.get("style", {}),
            image_urls=product.images,
            source_url=aliexpress_url,
            price=product.price,
        )
    except pdf_generator.PDFGenerationError as e:
        await interaction.followup.send(f"Couldn't generate the PDF: {e}")
        return
    except Exception:
        log.exception("Unexpected PDF generation error")
        await interaction.followup.send("Something went wrong rendering the PDF.")
        return

    try:
        await interaction.followup.send(
            content=f"Here's the spec sheet for **{product.title}**:",
            file=discord.File(pdf_path, filename="spec_sheet.pdf"),
        )
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass


@tree.command(name="current_template", description="Show info about the currently active spec sheet template")
async def current_template(interaction: discord.Interaction):
    stored = storage.get_template(_scope_id(interaction))
    if not stored:
        await interaction.response.send_message("No template is set yet. Use `/set_template` to upload one.")
        return

    schema = stored["schema"]
    section_summary = "\n".join(
        f"- **{s['name']}**: {', '.join(s['fields'])}" for s in schema["sections"]
    )
    await interaction.response.send_message(
        f"Active template (from `{stored['source_filename']}`):\n\n"
        f"**{schema.get('document_title', 'Spec Sheet')}**\n{section_summary}"
    )


@tree.command(name="reset_template", description="Clear the currently active spec sheet template")
async def reset_template(interaction: discord.Interaction):
    deleted = storage.delete_template(_scope_id(interaction))
    if deleted:
        await interaction.response.send_message("Template cleared. Upload a new one with `/set_template`.")
    else:
        await interaction.response.send_message("There was no template set.")


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error("Command error: %s\n%s", error, "".join(traceback.format_exception(error)))
    message = "An unexpected error occurred while running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message)
        else:
            await interaction.response.send_message(message)
    except Exception:
        pass


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
