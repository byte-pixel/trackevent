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

```powershell
python main.py --days 14 --region sf_bay --headless
```

Outputs:
- `out/events.json`
- `out/events.csv`

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

## Notes
- The Claude Agent SDK requires Claude Code to be installed as its runtime
- The `--headless` flag is ignored (SDK manages its own browser context)

