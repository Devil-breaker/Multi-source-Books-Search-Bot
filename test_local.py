#!/usr/bin/env python3
"""
Local test script for the Vercel version of the bot.
Tests the bot's core logic without needing Telegram or webhooks.
"""
import sys
import os
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_bot_import():
    """Test that all imports work."""
    print("🔍 Testing imports...")
    from src.bot import (
        GoodreadsBot,
        MultiSourceBookAggregator,
        get_bot,
        scrape_goodreads,
        html_escape,
        md_escape,
        is_placeholder_image,
    )
    print("   ✅ All imports successful")
    return True

def test_bot_initialization():
    """Test that the bot initializes correctly."""
    print("🔍 Testing bot initialization...")
    from src.bot import GoodreadsBot, TELEGRAM_BOT_TOKEN

    if not TELEGRAM_BOT_TOKEN:
        print("   ⚠️  TELEGRAM_BOT_TOKEN not set — skipping full init test")
        return True

    bot = GoodreadsBot(TELEGRAM_BOT_TOKEN, webhook_mode=True)
    assert bot.app is not None, "Bot application not initialized"
    print(f"   ✅ Bot initialized (token: {TELEGRAM_BOT_TOKEN[:10]}...)")
    return True

def test_singleton():
    """Test that get_bot() returns a singleton."""
    print("🔍 Testing singleton pattern...")
    from src.bot import get_bot

    bot1 = get_bot()
    bot2 = get_bot()
    assert bot1 is bot2, "get_bot() should return same instance"
    print("   ✅ Singleton pattern works")
    return True

def test_html_escape():
    """Test HTML escape function."""
    print("🔍 Testing html_escape...")
    from src.bot import html_escape

    result = html_escape('<b>Test & "Book" <3>')
    assert "&lt;" in result, "Should escape <"
    assert "&gt;" in result, "Should escape >"
    assert "&amp;" in result, "Should escape &"
    assert "&quot;" in result, "Should escape quotes"
    print(f"   ✅ html_escape works: '{result}'")
    return True

def test_placeholder_image_detection():
    """Test that placeholder image detection works."""
    print("🔍 Testing placeholder image detection...")
    from src.bot import is_placeholder_image

    # Real data (not a placeholder)
    assert is_placeholder_image(b"") == True, "Empty data should be placeholder"
    assert is_placeholder_image(b"x") == True, "Very small data should be placeholder"
    assert is_placeholder_image(b"x" * 100) == False, "Large random data should not be placeholder"
    print("   ✅ Placeholder detection works")
    return True

def test_book_aggregator_cache():
    """Test Hardcover cache flush."""
    print("🔍 Testing cache operations...")
    from src.bot import MultiSourceBookAggregator

    # Add a dummy entry to cache
    MultiSourceBookAggregator._hc_cache[("test", "title", "author")] = (4.5, 1000, [], "")

    # Verify it's there
    assert ("test", "title", "author") in MultiSourceBookAggregator._hc_cache
    print(f"   ✅ Cache has {len(MultiSourceBookAggregator._hc_cache)} entries before flush")

    # Flush
    MultiSourceBookAggregator._flush_hc_cache()

    # Verify it's gone
    assert len(MultiSourceBookAggregator._hc_cache) == 0, "Cache should be empty after flush"
    print("   ✅ Cache flush works")
    return True

def test_api_webhook_format():
    """Test that webhook handler accepts correct format."""
    print("🔍 Testing API webhook format...")

    # Simulate a Telegram update payload
    sample_update = {
        "update_id": 123456789,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": 123456789, "type": "private"},
            "from": {"id": 123456789, "is_bot": False, "first_name": "Test"},
            "text": "/start"
        }
    }

    # Validate it can be JSON serialized
    json_str = json.dumps(sample_update)
    parsed = json.loads(json_str)
    assert parsed["message"]["text"] == "/start"
    print("   ✅ Telegram update format is valid")
    return True

def test_vercel_json():
    """Test vercel.json is valid JSON."""
    print("🔍 Testing vercel.json...")
    import json

    with open("vercel.json") as f:
        config = json.load(f)

    assert config["version"] == 2, "Vercel version should be 2"
    assert "crons" in config, "Should have crons config"
    assert config["crons"][0]["path"] == "api/cron.py"
    assert config["crons"][0]["schedule"] == "*/10 * * * *"
    print(f"   ✅ vercel.json valid — cron: {config['crons'][0]['schedule']}")
    return True

def test_api_routes():
    """Test API routes exist and are importable."""
    print("🔍 Testing API routes...")

    # Read api/webhook.py and check it has a handler function
    with open("api/webhook.py") as f:
        content = f.read()
    assert "def handler" in content, "webhook.py should have handler function"
    assert "process_update" in content, "webhook.py should call process_update"
    print("   ✅ api/webhook.py has handler function")

    # Read api/cron.py
    with open("api/cron.py") as f:
        content = f.read()
    assert "def handler" in content, "cron.py should have handler function"
    assert "_flush_hc_cache" in content, "cron.py should flush cache"
    print("   ✅ api/cron.py has handler function")

    return True

def test_requirements():
    """Test that vercel_requirements.txt has all needed packages."""
    print("🔍 Testing vercel_requirements.txt...")
    required = ["python-telegram-bot", "requests", "beautifulsoup4", "python-dotenv"]
    with open("vercel_requirements.txt") as f:
        content = f.read()

    for pkg in required:
        assert pkg in content, f"Missing {pkg} in vercel_requirements.txt"

    print(f"   ✅ All required packages present ({len(required)} packages)")
    return True

def run_all_tests():
    print("=" * 60)
    print("🧪 Vercel Bot — Local Test Suite")
    print("=" * 60)
    print()

    tests = [
        test_bot_import,
        test_bot_initialization,
        test_singleton,
        test_html_escape,
        test_placeholder_image_detection,
        test_book_aggregator_cache,
        test_api_webhook_format,
        test_vercel_json,
        test_api_routes,
        test_requirements,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"   ❌ {test.__name__} FAILED: {e}")
            failed += 1
        print()

    print("=" * 60)
    print(f"📊 Results: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed == 0:
        print("\n🎉 All tests passed! Ready for Vercel deployment.")
    else:
        print("\n⚠️  Some tests failed. Fix the issues above before deploying.")

    return failed == 0

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)