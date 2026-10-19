"""
WebOrbit — configuration.

Everything here is read from environment variables (see .env.example).
No API key is ever hardcoded. If OPENROUTER_API_KEY is empty, the whole
app runs in deterministic LOCAL DEMO FALLBACK mode instead of pretending
to call an LLM.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv  # optional, only used if python-dotenv is installed
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DATA_DIR = BASE_DIR.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.json"
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR = DATA_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

LLM_MODE = "OPENROUTER FREE" if OPENROUTER_API_KEY else "LOCAL DEMO FALLBACK"

HOST = os.getenv("WEBORBIT_HOST", "127.0.0.1")
PORT = int(os.getenv("WEBORBIT_PORT", "8000"))

VIEWPORT = {"width": 1280, "height": 800}
SCREENCAST_FPS = float(os.getenv("WEBORBIT_FPS", "2.5"))  # frames/sec streamed to the UI
LOGIN_WAIT_TIMEOUT_S = int(os.getenv("WEBORBIT_LOGIN_TIMEOUT", "180"))
CONFIRM_DOMAIN_TIMEOUT_S = int(os.getenv("WEBORBIT_CONFIRM_TIMEOUT", "60"))
STEP_TIMEOUT_S = int(os.getenv("WEBORBIT_STEP_TIMEOUT", "20"))

# Site aliases the deterministic parser + planner understand out of the box.
# The demo_* entries point at the bundled local demo site (mounted at /demo
# on this same server) so the agent is always testable without the internet.
SITE_ALIASES = {
    "gmail": "https://mail.google.com/",
    "google": "https://www.google.com/",
    "youtube": "https://www.youtube.com/",
    "linkedin": "https://www.linkedin.com/",
    "amazon": "https://www.amazon.in/",
    "demo mail": "/demo/mail",
    "demo login": "/demo/login",
    "demo search": "/demo/search",
    "demo form": "/demo/forms",
    "demo table": "/demo/table",
    "demo download": "/demo/download",
    "demo delayed": "/demo/delayed",
    "demo shadow": "/demo/shadow",
    "demo long": "/demo/long",
    "demo home": "/demo/",
}

# Domains the agent may visit without asking first. Anything else triggers
# a "this site isn't on the allow-list, continue anyway?" confirmation,
# per the pre-execution guardrail spec.
ALLOWED_DOMAIN_SUFFIXES = [
    "google.com", "youtube.com", "linkedin.com", "amazon.in", "amazon.com",
    "127.0.0.1", "localhost",
]

# URL/DOM fingerprints used to recognise a page is asking the human to log in.
LOGIN_URL_HINTS = [
    "accounts.google.com", "/login", "/signin", "/sign-in", "checkpoint",
    "authwall", "/uas/login",
]


def mode_label(plan_source) -> str:
    """Human-readable planning mode recorded in task history. "llm" means the
    plan really came from OpenRouter; anything else (no key, or OpenRouter
    failed and the deterministic planner took over) is the local fallback."""
    return "OpenRouter Free" if plan_source == "llm" else "Local Demo Fallback"
