# api/cron.py
"""
Vercel Cron Job endpoint.
- Flushes Hardcover API cache (daily housekeeping)
- Acts as keep-warm heartbeat (prevents cold starts)
- Runs every 10 minutes (configured in vercel.json)
"""
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Optional extra secret for cron verification (in addition to X-Vercel-Cron header)
CRON_SECRET = os.getenv('CRON_SECRET', '')

def handler(request):
    """Vercel Cron handler."""

    # Verify it's actually from Vercel Cron (X-Vercel-Cron header is auto-set by Vercel)
    vercel_cron_header = request.headers.get('x-vercel-cron', '')
    if not vercel_cron_header:
        logger.warning('Cron request missing X-Vercel-Cron header')
        return {"statusCode": 403, "body": "Forbidden"}

    # Extra secret verification (optional)
    if CRON_SECRET:
        provided = request.headers.get('x-cron-secret', '')
        if provided != CRON_SECRET:
            return {"statusCode": 403, "body": "Forbidden"}

    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from src.handlers import get_bot
        from src.aggregator import MultiSourceBookAggregator

        bot = get_bot()

        # Flush Hardcover cache
        MultiSourceBookAggregator._flush_hc_cache()
        cache_flushed = True

        logger.info('Cron job ran successfully — Hardcover cache flushed')

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "ok": True,
                "status": "ok",
                "cache_flush": cache_flushed,
                "message": "Cron job completed successfully"
            })
        }
    except Exception as e:
        logger.error(f'Cron job failed: {e}')
        return {
            "statusCode": 500,
            "body": json.dumps({
                "ok": False,
                "error": str(e)
            })
        }