import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    anakin_api_key: str = os.getenv("ANAKIN_API_KEY", "").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    browser_mode: str = os.getenv("BROWSER_MODE", "auto").strip().lower()
    browser_headless: bool = _bool("BROWSER_HEADLESS", True)
    max_steps: int = int(os.getenv("MAX_STEPS", "40"))
    anakin_cdp_url: str = os.getenv(
        "ANAKIN_CDP_URL", "wss://api.anakin.io/v1/browser-connect"
    ).strip()


settings = Settings()
