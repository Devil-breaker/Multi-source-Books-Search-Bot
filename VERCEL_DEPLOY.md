# Deploy to Vercel

This guide walks you through deploying the Multi-Source Books Search Bot to Vercel.

## Prerequisites

- [Vercel account](https://vercel.com/signup) (free)
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- GitHub account (optional, for auto-deploy)

## Deployment Steps

### 1. Push Code to GitHub

The Vercel deployment will pull from GitHub (or you can deploy directly using the Vercel CLI).

```bash
git add .
git commit -m "Add Vercel deployment wrapper"
git push origin main
```

### 2. Deploy via Vercel CLI

```bash
npm i -g vercel
vercel login
vercel --prod
```

Follow the prompts:
- **Framework Preset**: Other
- **Build Command**: leave blank (Python has no build step)
- **Output Directory**: leave blank
- **Install Command**: `pip install -r vercel_requirements.txt`

### 3. Set Environment Variables

In the Vercel dashboard → your project → **Settings → Environment Variables**, add:

| Name | Value | Notes |
|------|-------|-------|
| `TELEGRAM_BOT_TOKEN` | `6070360797:...` | From @BotFather |
| `GOOGLE_BOOKS_API_KEY` | `AIzaSy...` | Optional, for richer metadata |
| `HARDCOVER_API_KEY` | `hc_pat_...` | Optional, for ratings |
| `WEBHOOK_SECRET` | (random string) | Optional: set to secure your webhook |
| `CRON_SECRET` | (random string) | Optional: extra cron security |

### 4. Register the Webhook with Telegram

After deployment, Vercel gives you a URL like:
`https://your-project.vercel.app`

**Set the webhook URL** by visiting this URL in your browser:

```
https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://your-project.vercel.app/api/webhook
```

Replace `<YOUR_TELEGRAM_BOT_TOKEN>` with your actual token.

**Verify your webhook** is set:
```
https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/getWebhookInfo
```

### 5. Test the Bot

Send `/start` to your Telegram bot — you should see the welcome message!

Send `/search Harry Potter` — results should appear with inline buttons.

### 6. Test the Cron Endpoint

Visit `https://your-project.vercel.app/api/cron` — you should see:
```json
{"ok": true, "status": "ok", "cache_flush": true, "message": "Cron job completed successfully"}
```

---

## How It Works

### Webhook Mode (vs Polling)

| Mode | How it works | Vercel compatible |
|------|-------------|------------------|
| **Polling** | Bot continuously calls `getUpdates` | ❌ No (needs always-on server) |
| **Webhook** | Telegram sends POST to your URL | ✅ Yes (serverless functions) |

The original `goodreads_bot_advanced.py` uses polling (for local/Docker use). The Vercel deployment uses webhooks — Telegram pushes updates to `/api/webhook`, which processes them instantly.

### Cron Job (Every 10 Minutes)

The `vercel.json` configures a cron job that calls `/api/cron` every 10 minutes:

- **Heartbeat**: Keeps the Vercel function warm, reducing cold starts
- **Cache Flush**: Clears the Hardcover API in-memory cache (fresh data each day)

### Architecture Diagram

```
                    ┌─────────────────────────────────────┐
                    │           Vercel Cloud              │
                    │                                     │
Telegram ──POST────▶│ api/webhook.py ──▶ src/bot.py      │
                    │      (webhook)       (GoodreadsBot) │
                    │                                     │
Vercel Cron ──POST─▶│ api/cron.py ──▶ flush cache         │
      (every 10min) │      (heartbeat + cleanup)          │
                    └─────────────────────────────────────┘
```

### Cold Start Handling

Vercel serverless functions "sleep" after inactivity. The 10-minute cron keeps the function warm:

- First request after sleep → cold start (~2-5s)
- Subsequent requests → warm (~100-300ms)

The `get_bot()` singleton ensures the bot is initialized once per warm instance.

---

## Important Notes

### Webhook Secret (Recommended)

Set `WEBHOOK_SECRET` in Vercel environment variables to prevent unauthorized webhook calls:

1. Generate a random string: `openssl rand -hex 32`
2. Add it as `WEBHOOK_SECRET` in Vercel dashboard
3. When registering the webhook, include it:
```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-project.vercel.app/api/webhook&secret_token=YOUR_SECRET
```

### Timeout Limits

Vercel Hobby plan: **10 seconds** per serverless function invocation.

The bot handlers are designed to complete well within this limit:
- Search API calls: ~2-3 seconds
- Cover downloads: ~1-2 seconds
- Total per user request: < 10 seconds

### Environment Variables on Vercel

**Do NOT** put `.env` in version control. Use Vercel's dashboard to set environment variables for each environment (Production, Preview, Development).

### Keep the Original Bot

`goodreads_bot_advanced.py` is **untouched**. You can still run it locally:

```bash
python goodreads_bot_advanced.py
```

### Custom Domain (Optional)

In Vercel dashboard → **Settings → Domains**, add a custom domain. Then update your Telegram webhook URL:

```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-domain.com/api/webhook
```

---

## Troubleshooting

### Bot not responding?
1. Check `/api/cron` — does it return `{"ok": true}`?
2. Check `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` — is webhook set?
3. Check Vercel function logs in the dashboard

### Cron not running?
1. Ensure `vercel.json` has the correct `crons` configuration
2. Check the cron runs in Vercel dashboard → **Functions → Cron Jobs**
3. Vercel Hobby plan: cron jobs run approximately every 10 minutes (not guaranteed exact)

### Cold start delays?
- Upgrade to Vercel Pro for more reliable performance
- The 10-minute cron interval keeps the function warm between requests

### Import errors?
- Ensure `vercel_requirements.txt` is in the project root
- Ensure `src/bot.py` exists with all bot logic
- Rebuild: `vercel --prod --force`