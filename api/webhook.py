# api/webhook.py
from http.server import BaseHTTPRequestHandler
import json
import os

def handler(request):
    """Vercel Python handler."""
    if request.method != 'POST':
        return {"statusCode": 405, "body": "Method Not Allowed"}

    try:
        raw_update = json.loads(request.body)
    except:
        return {"statusCode": 400, "body": "Bad Request"}

    # Verify secret
    secret = os.getenv('WEBHOOK_SECRET', '')
    if secret:
        provided = request.headers.get('x-telegram-bot-api-secret-token', '')
        if provided != secret:
            return {"statusCode": 403, "body": "Forbidden"}

    import sys
    import os as osmod
    sys.path.insert(0, osmod.path.join(osmod.path.dirname(__file__), '..'))
    from src.bot import get_bot

    bot = get_bot()
    bot.process_update(raw_update)

    return {"statusCode": 200, "body": json.dumps({"ok": True})}