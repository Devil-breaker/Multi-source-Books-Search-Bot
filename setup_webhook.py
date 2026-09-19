#!/usr/bin/env python3
"""
Telegram Webhook Setup Script for Vercel Deployment.

Usage:
    python setup_webhook.py

This script registers your Vercel URL as the Telegram bot's webhook endpoint.
"""

import requests
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
VERCEL_URL = os.getenv("VERCEL_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Missing TELEGRAM_BOT_TOKEN in .env file")
        return

    if not VERCEL_URL:
        print("❌ Missing VERCEL_URL in .env file")
        print("   After deploying to Vercel, set VERCEL_URL to your project URL")
        print("   e.g., https://my-books-bot.vercel.app")
        return

    # Build webhook URL
    webhook_url = f"{VERCEL_URL.rstrip('/')}/api/webhook"
    print(f"📡 Setting webhook URL to: {webhook_url}")

    # Build request params
    params = {"url": webhook_url}
    if WEBHOOK_SECRET:
        params["secret_token"] = WEBHOOK_SECRET

    # Register webhook
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    response = requests.get(f"{base_url}/setWebhook", params=params, timeout=15)

    if response.status_code == 200:
        data = response.json()
        if data.get("ok"):
            print("✅ Webhook registered successfully!")
        else:
            print(f"❌ Telegram API error: {data}")
    else:
        print(f"❌ HTTP error: {response.status_code} - {response.text}")

    # Verify webhook
    print("\n🔍 Verifying webhook...")
    verify_resp = requests.get(f"{base_url}/getWebhookInfo", timeout=15)
    if verify_resp.status_code == 200:
        info = verify_resp.json()
        if info.get("ok"):
            webhook_info = info.get("result", {})
            print(f"   URL: {webhook_info.get('url', 'NOT SET')}")
            print(f"   Pending updates: {webhook_info.get('pending_update_count', 0)}")
            print(f"   Last error: {webhook_info.get('last_error_message', 'None')}")
        else:
            print(f"❌ Verify failed: {info}")
    else:
        print(f"❌ Could not verify: {verify_resp.status_code}")


if __name__ == "__main__":
    print("=" * 60)
    print("📚 Multi-Source Books Bot — Webhook Setup")
    print("=" * 60)
    print()
    main()
    print()
    print("=" * 60)
    print("Next steps:")
    print("1. Test the bot: send /start to your Telegram bot")
    print("2. Test search: send /search Harry Potter")
    print("3. Check /api/cron to verify cron is working")
    print("=" * 60)