import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return default

    return raw.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class Settings:
    groq_api_key: str = os.getenv(
        "GROQ_API_KEY",
        "",
    ).strip()

    groq_model: str = os.getenv(
        "GROQ_MODEL",
        "qwen/qwen3.8-27b",
    ).strip()

    anakin_api_key: str = os.getenv(
        "ANAKIN_API_KEY",
        "",
    ).strip()

    browser_mode: str = os.getenv(
        "BROWSER_MODE",
        "auto",
    ).strip().lower()

    browser_headless: bool = _bool(
        "BROWSER_HEADLESS",
        True,
    )

    max_steps: int = int(
        os.getenv(
            "MAX_STEPS",
            "40",
        )
    )

    max_history_turns: int = int(
        os.getenv(
            "MAX_HISTORY_TURNS",
            "3",
        )
    )

    anakin_cdp_url: str = os.getenv(
        "ANAKIN_CDP_URL",
        "wss://api.anakin.io/v1/browser-connect",
    ).strip()


settings = Settings()
