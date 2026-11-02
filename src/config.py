import os
from pathlib import Path
from typing import Dict, List, Optional
import yaml


def load_env_file(env_path: Path) -> None:
    """Load key-value pairs from .env into os.environ if not already set."""
    if not env_path.is_file():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val


class SourceItem:
    def __init__(
        self,
        id: int,
        name: str,
        url: str,
        category: str = "general",
        priority: int = 1,
        lookup_query: Optional[str] = None,
        enabled: bool = True,
        access: str = "public",
    ):
        self.id = id
        self.name = name
        self.url = url
        self.category = category
        self.priority = priority
        self.lookup_query = lookup_query
        self.enabled = enabled
        self.access = access

    @classmethod
    def from_dict(cls, data: dict) -> "SourceItem":
        return cls(
            id=int(data.get("id")),
            name=str(data.get("name")),
            url=str(data.get("url") or ""),
            category=str(data.get("category", "general")),
            priority=int(data.get("priority", 1)),
            lookup_query=data.get("lookup_query"),
            enabled=bool(data.get("enabled", True)),
            access=str(data.get("access", "public")),
        )


class Config:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        load_env_file(self.base_dir / ".env")

        self.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.fb_cookies = os.environ.get("FB_COOKIES", "").strip()

        try:
            self.check_interval_seconds = int(
                os.environ.get("CHECK_INTERVAL_SECONDS", "300")
            )
        except ValueError:
            self.check_interval_seconds = 300

        try:
            self.request_delay_seconds = float(
                os.environ.get("REQUEST_DELAY_SECONDS", "3.0")
            )
        except ValueError:
            self.request_delay_seconds = 3.0

        auto_baseline_str = os.environ.get("AUTO_BASELINE_FIRST_RUN", "true").lower()
        self.auto_baseline_first_run = auto_baseline_str in ("true", "1", "yes")

        self.sources_file = self.base_dir / "sources.yaml"
        self.db_file = self.base_dir / "index.db"

    def parse_fb_cookies(self) -> Dict[str, str]:
        """Parse raw cookie header string into a dictionary."""
        cookies: Dict[str, str] = {}
        if not self.fb_cookies:
            return cookies
        for pair in self.fb_cookies.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookies[k.strip()] = v.strip()
        return cookies

    def load_sources(self) -> List[SourceItem]:
        """Load enabled sources with valid URLs from sources.yaml or APP_SOURCES_DATA env var."""
        raw_env = os.environ.get("APP_SOURCES_DATA", "").strip()
        if raw_env:
            data = yaml.safe_load(raw_env)
        elif self.sources_file.is_file():
            with open(self.sources_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        else:
            raise FileNotFoundError(
                f"Sources not found: neither {self.sources_file} nor APP_SOURCES_DATA env var is configured."
            )

        if not data or not isinstance(data, dict):
            return []

        raw_sources = data.get("sources", [])
        sources: List[SourceItem] = []
        for item in raw_sources:
            src = SourceItem.from_dict(item)
            if src.enabled and src.url:
                sources.append(src)
        return sources
