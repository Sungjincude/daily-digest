# Daily Digest Bot

Sends 10 curated articles every day at 8 AM Greece time (05:00 UTC) via Telegram.

## Setup

### 1. Create a Telegram Bot
- Message **@BotFather** on Telegram
- Send `/newbot`, follow prompts
- Copy the **bot token**

### 2. Get your Chat ID
- Message **@userinfobot** on Telegram
- It will reply with your numeric **Chat ID**

### 3. Get a News API Key
- Sign up at [newsapi.org](https://newsapi.org/register) (free tier: 100 requests/day)
- Copy your **API key**

### 4. Get an OpenRouter API Key (optional — for AI curation)
- Sign up at [openrouter.ai](https://openrouter.ai)
- Free tier models available (e.g. Llama 3.3 70B)
- Copy your **API key**

### 5. Push to GitHub and configure secrets
```bash
cd daily-digest
git init
git add .
git commit -m "Initial commit"

# Create a new repo on GitHub, then:
git remote add origin git@github.com:YOUR_USER/daily-digest.git
git push -u origin main
```

Then in your GitHub repository go to:
**Settings → Secrets and variables → Actions → New repository secret**

Add these secrets:

| Secret Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_CHAT_ID` | From userinfobot |
| `NEWS_API_KEY` | From newsapi.org |
| `OPENROUTER_API_KEY` | From openrouter.ai |
| `LLM_MODEL` | (optional) e.g. `meta-llama/llama-3.3-70b-instruct:free` |

### 6. Done!
The workflow runs daily at 8 AM Greece time. To test immediately, go to:
**Actions → Daily Digest → Run workflow**

## Without AI (works without OpenRouter)
Delete or leave the `OPENROUTER_API_KEY` secret empty. The bot falls back to rule-based curation.
