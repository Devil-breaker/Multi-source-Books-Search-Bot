# 📚 Multi-Source Books Search Bot

A Telegram bot that searches for books across multiple sources (Google Books, iTunes, Hardcover.app, StoryGraph) and delivers rich results including covers, ratings, descriptions, and metadata — all in your chat.

## ✨ Features

- **Multi-source search** — queries Google Books, iTunes, Hardcover.app, and StoryGraph simultaneously
- **High-resolution covers** — fetches cover art from iTunes
- **Rich book details** — ratings, reviews, ISBN, page count, publication year, genres, descriptions
- **Cover downloads** — send book covers directly to your chat
- **Rating aggregation** — community ratings from Hardcover.app and StoryGraph
- **Direct links** — inline links to Google Books and Goodreads

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/start` | Show welcome message |
| `/help` | Show help and usage guide |
| `/search <query>` | Search for books by title, author, or ISBN |

## 🚀 Setup

### Prerequisites

- Python 3.9+
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- (Optional) [Google Books API Key](https://console.cloud.google.com/apis/library/books.googleapis.com) for richer metadata
- (Optional) [Hardcover.app API Key](https://hardcover.app/settings/api) for community ratings

### Installation

```bash
# Clone the repository
git clone https://github.com/Devil-breaker/Multi-source-Books-Search-Bot.git
cd Multi-source-Books-Search-Bot

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp env.example .env
```

Edit `.env` and add your credentials:

```env
TELEGRAM_BOT_TOKEN=123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
GOOGLE_BOOKS_API_KEY=your_google_books_api_key_here
HARDCOVER_API_KEY=your_hardcover_api_key_here
```

### Run

```bash
python goodreads_bot_advanced.py
```

### Run with Docker

```bash
docker build -t books-bot .
docker run --env-file .env books-bot
```

## 📁 Project Structure

```
.
├── goodreads_bot_advanced.py   # Main bot (all-in-one)
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Docker image
├── .env                        # Environment variables (git-ignored)
├── env.example                 # Environment template
└── .gitignore                  # Git ignore rules
```

## 🛠️ Dependencies

- [python-telegram-bot](https://python-telegram-bot.org/) — Telegram Bot API
- [requests](https://docs.python-requests.org/) — HTTP client
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing
- [python-dotenv](https://pypi.org/project/python-dotenv/) — .env support
- [lxml](https://lxml.de/) — XML/HTML parser
- [storygraph-api](https://pypi.org/project/storygraph-api/) — StoryGraph ratings