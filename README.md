# TrackEvents (Luma + Judgment Labs)

Scrape **Luma** starting from `https://lu.ma/` to collect **SF Bay Area** events happening in the **next 14 days**, filter to events aligned with Judgment Labs' field (agent reliability / monitoring / observability / evaluation), and export results to JSON + CSV.

Built with the **[Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview)** — the same tools and agent loop that power Claude Code, now as a library.

## Prereqs
- Windows + Python 3.12 (you already have a `venv/` in this repo)
- [Claude Code](https://code.claude.com/docs/en/setup) installed (runtime for Agent SDK)
- An Anthropic API key available as `ANTHROPIC_API_KEY`
- A Judgment Labs API key available as `JUDGMENT_API_KEY` (for online monitoring)

## Setup

Install Claude Code (required runtime):

```powershell
winget install Anthropic.ClaudeCode
```

Activate venv (PowerShell):

```powershell
.\venv\Scripts\Activate.ps1
```

Install deps:

```powershell
python -m pip install -r requirements.txt
```

## Run

### CLI

```powershell
python main.py --days 14 --region sf_bay --headless
```

Outputs:
- `out/events.json`
- `out/events.csv`

### Slack Bot

Run the Slack bot to get events via mentions:

```powershell
python slack_bot.py
```

**Setup:**

1. Create a Slack app at https://api.slack.com/apps
2. Enable **Socket Mode** in your app settings (under "Socket Mode")
3. Create an App-Level Token with `connections:write` scope (for Socket Mode)

4. Enable **Event Subscriptions** and subscribe to the `app_mentions` event
5. Add the following OAuth Bot Token Scopes (under "OAuth & Permissions"):
   - `chat:write` - Send messages
   - `im:read` - Read direct messages
6. Install the app to your workspace (under "Install App")
7. Copy the **Bot Token** (starts with `xoxb-`) and **App-Level Token** (starts with `xapp-`)
8. Add to your `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb...
   SLACK_APP_TOKEN=xapp...

   ```

**Usage:**

- Mention the bot in any channel: `@YourBotName` - it will scrape and post relevant events
- The bot responds with formatted event cards showing:
  - Event title (clickable link)
  - Date and time
  - Location
  - Relevance score
  - Why it's relevant
  - Matched topics/keywords

## Online Monitoring (Judgment Labs)

This project includes **live agent behavior monitoring** via [Judgment Labs](https://judgmentlabs.ai):

- **Tracing**: All LLM calls, tool invocations, and function spans are logged
- **Async Evaluation**: Automatic scoring of agent outputs for relevancy and faithfulness
- **Dashboard**: View traces and evaluation results at your Judgment Labs dashboard

Set your API key in `.env`:
```
JUDGMENT_API_KEY=your-judgment-labs-api-key
```

## Architecture

The agent uses the Claude Agent SDK with built-in `WebFetch` and `WebSearch` tools:

1. **URL Collection**: Agent navigates Luma SF page and extracts event URLs
2. **Event Extraction**: For each URL, agent fetches and parses event details
3. **Filtering**: Events filtered by date (next N days), region (SF Bay), and relevance (keywords)
4. **Monitoring**: All agent actions traced via Judgeval; outputs scored for relevancy

## Deployment to Fly.io (Free Tier)

Deploy the Slack bot to Fly.io for 24/7 operation:

### Prerequisites
1. Install [Fly CLI](https://fly.io/docs/hands-on/install-flyctl/)
2. Sign up for a free Fly.io account: `fly auth signup`
3. Login: `fly auth login`

### Deploy

1. **Initialize Fly.io app** (first time only):
   ```powershell
   fly launch --no-deploy
   ```
   - Choose an app name (or use default)
   - Choose a region (e.g., `iad` for Washington D.C.)

2. **Set environment variables** (choose one method):

   **Option A: Import from .env file** (recommended):
   ```powershell
   # PowerShell syntax (strips BOM, empty lines, and comments)
   Get-Content .env -Encoding UTF8 | 
     Where-Object { $_ -match '^\s*[^#]' -and $_ -match '=' } | 
     ForEach-Object { $_ -replace '^\ufeff','' -replace '^\s+','' -replace '\s+$','' } | 
     fly secrets import
   ```
   
   **Option B: Manual fix for BOM issue**:
   ```powershell
   # Create a clean version without BOM
   $content = Get-Content .env -Raw -Encoding UTF8
   $content = $content -replace '\ufeff',''
   $content | Out-File .env.clean -Encoding UTF8 -NoNewline
   Get-Content .env.clean | fly secrets import
   ```
   
   > **Note**: If you get a BOM error (`\ufeff`), your .env file has a Byte Order Mark. The commands above will strip it. Alternatively, re-save your .env file as UTF-8 without BOM in VS Code (click encoding in bottom-right → "Save with Encoding" → "UTF-8").
   
   **Option B: Set individually**:
   ```powershell
   fly secrets set ANTHROPIC_API_KEY=your-key
   fly secrets set JUDGMENT_API_KEY=your-key
   fly secrets set SLACK_BOT_TOKEN=xoxb-your-token
   fly secrets set SLACK_APP_TOKEN=xapp-your-token
   ```
   
   **Option C: Set all at once**:
   ```powershell
   fly secrets set ANTHROPIC_API_KEY=your-key JUDGMENT_API_KEY=your-key SLACK_BOT_TOKEN=xoxb-your-token SLACK_APP_TOKEN=xapp-your-token
   ```
   
   > **Note**: The `.env` file format should be `KEY=VALUE` (one per line, no spaces around `=`)

3. **Deploy**:
   ```powershell
   fly deploy
   ```

4. **Check status**:
   ```powershell
   fly status
   fly logs
   ```

### Fly.io Free Tier Limits
- **3 shared-cpu-1x VMs** (256MB RAM each)
- **3GB persistent volumes**
- **160GB outbound data transfer/month**
- **Unlimited inbound data**

The Slack bot should fit comfortably within these limits.

### Troubleshooting

- **View logs**: `fly logs`
- **SSH into machine**: `fly ssh console`
- **Restart app**: `fly apps restart trackevents-bot`
- **Scale**: The free tier allows 1 machine, which is sufficient for the bot

## Notes
- The Claude Agent SDK may work without Claude Code in cloud environments (uses WebFetch directly)
- The `--headless` flag is ignored (SDK manages its own browser context)
- Browser profile is stored in `out/browser_profile/` (ephemeral on Fly.io)

