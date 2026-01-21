# TrackEvents (Luma + Judgment Labs)

Scrape **Luma** starting from `https://lu.ma/` to collect **SF Bay Area** events happening in the **next 14 days**, filter to events aligned with Judgment Labs’ field (agent reliability / monitoring / observability / evaluation), and export results to JSON + CSV.

## Prereqs
- Windows + Python 3.12 (you already have a `venv/` in this repo)
- An Anthropic API key available as `ANTHROPIC_API_KEY`
- A Judgment Labs API key available as `JUDGMENT_API_KEY` (for online monitoring)

## Setup

Activate venv (PowerShell):

```powershell
.\venv\Scripts\Activate.ps1
```

Install deps:

```powershell
python -m pip install -r requirements.txt
python -m playwright install
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

## Notes
- If Luma shows an interstitial or blocks automation, run without `--headless` and complete any one-time steps; the script persists a local browser profile in `out/browser_profile/`.

