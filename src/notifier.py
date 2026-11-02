from datetime import datetime, timezone
import html
import logging
import re
import time
from typing import Optional, Tuple
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    @property
    def is_configured(self) -> bool:
        return bool(
            self.bot_token
            and self.chat_id
            and self.bot_token != "your_bot_token_here"
        )

    def test_connection(self) -> Tuple[bool, str]:
        """Test Telegram Bot Token and Chat ID validity."""
        if not self.is_configured:
            return False, "Telegram Bot Token or Chat ID is not configured in .env"

        try:
            # 1. Test Bot Token with getMe
            resp = requests.get(f"{self.api_base}/getMe", timeout=10)
            data = resp.json()
            if not data.get("ok"):
                return False, f"Invalid bot token: {data.get('description', 'Unknown error')}"
            bot_username = data.get("result", {}).get("username", "Unknown")

            # 2. Test Chat ID with a ping message
            test_msg = (
                f"🔔 <b>Facebook Notifier Connected!</b>\n"
                f"Connected successfully with bot <code>@{bot_username}</code>."
            )
            sent, err = self.send_message(test_msg)
            if not sent:
                return (
                    False,
                    f"Bot token valid (@{bot_username}), but failed to message chat ID {self.chat_id}: {err}",
                )

            return (
                True,
                f"Success! Connected to @{bot_username} and verified chat ID {self.chat_id}",
            )
        except Exception as e:
            return False, f"Network error connecting to Telegram: {e}"

    def send_message(
        self,
        text: str,
        parse_mode: Optional[str] = "HTML",
        disable_web_page_preview: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        if not self.is_configured:
            return False, "Telegram is not configured"

        url = f"{self.api_base}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
            "link_preview_options": {"is_disabled": disable_web_page_preview},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()

            # Handle rate limiting (429)
            if resp.status_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 3)
                logger.warning(
                    f"Telegram rate limited. Waiting {retry_after}s before retry..."
                )
                time.sleep(min(retry_after, 10))
                resp = requests.post(url, json=payload, timeout=15)
                data = resp.json()

            if data.get("ok"):
                return True, None

            err_desc = data.get("description", "Unknown error")

            # Fallback for HTML entity parsing errors: strip HTML and send plaintext
            if parse_mode == "HTML" and "can't parse entities" in err_desc.lower():
                logger.warning(
                    f"HTML parse failed ({err_desc}). Retrying as plain text..."
                )
                plain_text = re.sub(r"<[^>]+>", "", text)
                payload["text"] = plain_text
                payload.pop("parse_mode", None)
                retry_resp = requests.post(url, json=payload, timeout=15)
                retry_data = retry_resp.json()
                if retry_data.get("ok"):
                    return True, None
                err_desc = retry_data.get("description", err_desc)

            logger.error(f"Telegram API error: {err_desc}")
            return False, err_desc
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False, str(e)

    def notify_post(
        self,
        source_name: str,
        category: str,
        post_url: str,
        message: Optional[str] = None,
        creation_time: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Format and send a post notification."""
        safe_name = html.escape(source_name)

        # Normalize category to a valid Telegram hashtag
        clean_tag = re.sub(r"[^\w]+", "_", category.strip()).strip("_")
        tag_line = f"🏷️ #{clean_tag}\n" if clean_tag else ""

        time_line = ""
        if creation_time:
            try:
                dt = datetime.fromtimestamp(
                    creation_time, tz=timezone.utc
                ).astimezone()
                time_str = dt.strftime("%Y-%m-%d %H:%M")
                time_line = f"🕒 <i>{time_str}</i>\n\n"
            except Exception:
                pass

        snippet = ""
        if message:
            clean_msg = message.strip()
            if len(clean_msg) > 600:
                clean_msg = clean_msg[:597] + "..."
            snippet = f"{html.escape(clean_msg)}\n\n"

        safe_url = html.escape(post_url)
        formatted_text = (
            f"📢 <b>{safe_name}</b> vừa đăng bài mới!\n"
            f"{tag_line}"
            f"{time_line}"
            f"{snippet}"
            f"🔗 <a href=\"{safe_url}\">Xem bài viết trên Facebook</a>"
        )

        return self.send_message(formatted_text, disable_web_page_preview=False)
