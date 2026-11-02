#!/usr/bin/env python3
"""
Routine Cache & State Synchronizer
Monitors configured source items and dispatches events.
"""
import argparse
import logging
import sys

from src.config import Config
from src.notifier import TelegramNotifier
from src.scraper import FacebookScraper
from src.service import NotifierService
from src.storage import Storage


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Routine Cache & State Synchronizer"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync across all enabled sources and exit (cron-friendly)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously in background with interval polling",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Record all currently existing items as seen without sending notifications",
    )
    parser.add_argument(
        "--test-telegram", "--test-dispatch",
        action="store_true",
        help="Send a test message to verify dispatcher configuration",
    )
    parser.add_argument(
        "--test-scraper", "--test-fetch",
        nargs="?",
        const="https://www.facebook.com/meta",
        help="Test fetching data from a source URL",
    )
    parser.add_argument(
        "--test-notify",
        nargs="?",
        const=2,
        type=int,
        help="Send a live test notification for a source ID",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print state cache statistics",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable detailed debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = Config()

    if args.test_telegram:
        print("\n--- Testing Telegram Bot Connection ---")
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        success, message = notifier.test_connection()
        if success:
            print(f"✅ {message}\n")
            sys.exit(0)
        else:
            print(f"❌ {message}\n")
            print("Please check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.")
            sys.exit(1)

    if args.test_scraper:
        target_url = args.test_scraper
        print(f"\n--- Testing Facebook Scraper on {target_url} ---")
        scraper = FacebookScraper(config.parse_fb_cookies())
        posts = scraper.scrape_page(target_url)
        if not posts:
            print("⚠️ No posts found or page could not be accessed.")
        else:
            print(f"✅ Extracted {len(posts)} post(s):")
            for i, p in enumerate(posts, 1):
                print(f"\n[{i}] Post ID: {p.post_id}")
                print(f"    URL:  {p.url}")
                print(f"    Time: {p.creation_time}")
                if p.message:
                    snippet = p.message[:140].replace("\n", " ")
                    print(f"    Text: {snippet}...")
        print()
        sys.exit(0)

    if args.stats:
        storage = Storage(config.db_file)
        stats = storage.get_stats()
        print("\n--- Notification Tracking Statistics ---")
        print(f"Total posts recorded:    {stats['total_posts']}")
        print(f"Unique sources tracked:  {stats['tracked_sources']}")
        print(f"Total alerts sent:       {stats['total_notified']}\n")
        sys.exit(0)

    service = NotifierService(config)

    if args.test_notify is not None:
        source_id = args.test_notify
        print(f"\n--- Sending Live Test Notification (Source ID {source_id}) ---")
        success, message = service.test_live_notification(source_id)
        if success:
            print(f"✅ {message}\n")
            print("👉 Please check your Telegram app now to verify the notification!")
            sys.exit(0)
        else:
            print(f"❌ {message}\n")
            sys.exit(1)

    if args.baseline:
        print("Recording baseline across all sources (no notifications will be sent)...")
        service.scan_all(force_baseline=True)
        sys.exit(0)

    if args.once:
        service.scan_all()
        sys.exit(0)

    if args.daemon:
        service.run_daemon()
        sys.exit(0)

    # Default action if no flags provided: show help or guide
    parser.print_help()


if __name__ == "__main__":
    main()
