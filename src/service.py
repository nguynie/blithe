from datetime import datetime, timezone
import html
import logging
import random
import signal
import sys
import time
from typing import Dict, Optional, Tuple

from src.config import Config, SourceItem
from src.notifier import TelegramNotifier
from src.scraper import FacebookPost, FacebookScraper
from src.storage import Storage

logger = logging.getLogger(__name__)


class NotifierService:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.storage = Storage(self.config.db_file)
        self.scraper = FacebookScraper(self.config.parse_fb_cookies())
        self.notifier = TelegramNotifier(
            self.config.telegram_bot_token,
            self.config.telegram_chat_id,
        )
        self.is_running = True

    def test_live_notification(self, source_id: int = 2) -> Tuple[bool, str]:
        """Fetch the latest post from a page and send a real test notification to Telegram."""
        sources = [s for s in self.config.load_sources() if s.id == source_id]
        if not sources:
            return False, f"Source ID {source_id} not found in sources.yaml"
        source = sources[0]

        posts = self.scraper.scrape_page(source.url)
        if not posts:
            return False, f"No recent posts could be fetched from {source.name} ({source.url})"

        p = posts[0]
        if not self.notifier.is_configured:
            return False, "Telegram credentials are not configured in .env"

        success, err = self.notifier.notify_post(
            source_name=f"[TEST ALERT] {source.name}",
            category=source.category,
            post_url=p.url,
            message=p.message,
            creation_time=p.creation_time,
        )
        if success:
            return True, f"Dispatched live test alert for post {p.post_id} from '{source.name}' to Telegram!"
        return False, f"Failed to send Telegram alert: {err}"

    def scan_source(
        self,
        source: SourceItem,
        force_baseline: bool = False,
        force_notify: bool = False,
    ) -> int:
        """
        Scan a single Facebook page source.
        Returns the count of newly detected and notified posts.
        """
        has_history = self.storage.has_source_history(source.id)
        posts = self.scraper.scrape_page(source.url)

        if not posts:
            logger.info(f"[{source.name}] No recent posts found or page empty.")
            return 0

        is_first_run = not has_history
        should_baseline = (
            force_baseline
            or (is_first_run and self.config.auto_baseline_first_run and not force_notify)
        )

        if should_baseline:
            logger.info(
                f"[{source.name}] First-run baseline: recording {len(posts)} existing post(s) without alerting."
            )
            for p in posts:
                self.storage.mark_seen(
                    source_id=source.id,
                    post_id=p.post_id,
                    post_url=p.url,
                    creation_time=p.creation_time,
                    message_snippet=p.message,
                    notified=False,
                )
            return 0

        unseen_posts = [
            p for p in posts if not self.storage.is_seen(source.id, p.post_id, p.url)
        ]

        if not unseen_posts:
            return 0

        unseen_posts.sort(key=lambda x: (x.creation_time or 0))

        new_count = 0
        for p in unseen_posts:
            logger.info(f"[{source.name}] New post detected! ID={p.post_id}")

            if self.notifier.is_configured:
                success, err = self.notifier.notify_post(
                    source_name=source.name,
                    category=source.category,
                    post_url=p.url,
                    message=p.message,
                    creation_time=p.creation_time,
                )
                if success:
                    logger.info(
                        f"[{source.name}] Sent Telegram notification for {p.post_id}"
                    )
                    self.storage.mark_seen(
                        source_id=source.id,
                        post_id=p.post_id,
                        post_url=p.url,
                        creation_time=p.creation_time,
                        message_snippet=p.message,
                        notified=True,
                    )
                    new_count += 1
                else:
                    logger.error(
                        f"[{source.name}] Failed to send Telegram alert for {p.post_id}: {err}. "
                        "Will retry on subsequent scan cycle."
                    )
            else:
                logger.warning(
                    f"[{source.name}] Telegram not configured. Skipping alert dispatch."
                )
                self.storage.mark_seen(
                    source_id=source.id,
                    post_id=p.post_id,
                    post_url=p.url,
                    creation_time=p.creation_time,
                    message_snippet=p.message,
                    notified=False,
                )
                new_count += 1

        return new_count

    def _check_and_alert_auth_issue(self) -> None:
        """
        If Facebook session/cookies encounter consecutive authentication or checkpoint
        challenges, dispatch an administrative alert to Telegram (at most once every 24 hours).
        """
        if not self.notifier.is_configured:
            return

        last_alert_str = self.storage.get_metadata("last_auth_alert_time")
        if last_alert_str:
            try:
                last_dt = datetime.fromisoformat(last_alert_str)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if elapsed < 86400:
                    logger.warning(
                        f"Facebook auth issue detected, but alert already sent {int(elapsed)}s ago. Suppressing."
                    )
                    return
            except Exception:
                pass

        details = self.scraper.last_auth_error or "Phiên đăng nhập bị chặn hoặc hết hạn"
        escaped_details = html.escape(details)
        admin_msg = (
            "<b>[Sync Notifier - System Alert]</b>\n\n"
            "Phát hiện lỗi phiên đăng nhập Facebook (Cookies đã hết hạn hoặc bị yêu cầu xác thực Checkpoint).\n"
            f"Chi tiết: <code>{escaped_details}</code>\n\n"
            "Vui lòng cập nhật <code>FB_COOKIES</code> trong GitHub Secrets (Settings > Secrets and variables > Actions) để tiếp tục nhận thông báo tự động!"
        )
        success, err = self.notifier.send_message(admin_msg)
        if success:
            logger.info("Dispatched Telegram admin alert for Facebook session failure.")
            self.storage.set_metadata(
                "last_auth_alert_time",
                datetime.now(timezone.utc).isoformat(),
            )
        else:
            logger.error(f"Failed to dispatch Telegram admin alert: {err}")

    def scan_all(
        self,
        force_baseline: bool = False,
        force_notify: bool = False,
    ) -> Dict[str, int]:
        """Scan all enabled sources sequentially with polite delays."""
        sources = self.config.load_sources()
        total_sources = len(sources)
        total_new_posts = 0

        logger.info(f"Starting scan across {total_sources} enabled sources...")

        for idx, source in enumerate(sources, start=1):
            if not self.is_running:
                logger.info("Scan stopped early by user/shutdown signal.")
                break

            logger.info(f"({idx}/{total_sources}) Checking {source.name}...")
            try:
                new_posts = self.scan_source(
                    source,
                    force_baseline=force_baseline,
                    force_notify=force_notify,
                )
                total_new_posts += new_posts
            except Exception as e:
                logger.error(f"Error scanning source {source.name} ({source.url}): {e}")

            if self.scraper.consecutive_auth_failures >= 5:
                self._check_and_alert_auth_issue()

            if idx < total_sources and self.config.request_delay_seconds > 0:
                jitter = random.uniform(0.8, 1.2) * self.config.request_delay_seconds
                time.sleep(jitter)

        if self.scraper.consecutive_auth_failures >= 5:
            self._check_and_alert_auth_issue()

        self.storage.sync_to_json()

        logger.info(
            f"Scan completed. Checked {total_sources} sources. Found {total_new_posts} new post(s)."
        )
        return {
            "scanned_sources": total_sources,
            "new_posts": total_new_posts,
        }

    def run_daemon(self) -> None:
        """Run continuous monitoring loop."""
        def handle_signal(sig, frame):
            logger.info("Termination signal received. Shutting down gracefully...")
            self.is_running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        logger.info(
            f"Notifier service started in daemon mode. Checking every {self.config.check_interval_seconds}s."
        )

        while self.is_running:
            self.scan_all()
            if not self.is_running:
                break

            logger.info(
                f"Sleeping for {self.config.check_interval_seconds} seconds until next scan cycle..."
            )
            slept = 0
            while slept < self.config.check_interval_seconds and self.is_running:
                time.sleep(1)
                slept += 1

        logger.info("Notifier service exited.")
