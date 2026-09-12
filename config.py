"""Central configuration loaded from environment variables (.env)."""
import os
from dotenv import load_dotenv

load_dotenv()

# LLM provider: "anthropic" or "openai"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# Storage
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "templates.db"))

# Uploads / scraping limits
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "15"))
MAX_TEMPLATE_CHARS = int(os.getenv("MAX_TEMPLATE_CHARS", "12000"))
SCRAPE_TIMEOUT_MS = int(os.getenv("SCRAPE_TIMEOUT_MS", "45000"))
SCRAPE_MAX_IMAGES = int(os.getenv("SCRAPE_MAX_IMAGES", "4"))

TEMP_DIR = os.getenv("TEMP_DIR", os.path.join(os.path.dirname(__file__), "temp"))
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
