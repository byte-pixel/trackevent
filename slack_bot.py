"""
Slack Bot for TrackEvents - Responds to mentions by scraping and posting relevant events
"""
from __future__ import annotations

import os
import sys
import logging
import threading
import traceback
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.config import SETTINGS
from src.judgment_topics import build_judgment_keyword_set
from src.luma_scrape_agent import scrape_luma_events_with_agent

load_dotenv()

# Set up logging to stdout (so it shows in fly logs)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    force=True  # Override any existing config
)
logger = logging.getLogger(__name__)

# Initialize Slack app
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# Track if scraping is in progress
scraping_lock = threading.Lock()
is_scraping = False


def format_event_for_slack(event) -> dict:
    """Format a single event as a Slack block."""
    # Handle Pydantic model
    if hasattr(event, "model_dump"):
        event_dict = event.model_dump()
    elif hasattr(event, "dict"):
        event_dict = event.dict()
    else:
        event_dict = event
    
    title = event_dict.get("title", "Untitled Event")
    url = str(event_dict.get("url", "")).strip()
    if not url or url == "None":
        url = "#"
    start_at = event_dict.get("start_at")
    venue = event_dict.get("venue", {})
    venue_raw = venue.get("raw", "TBD") if isinstance(venue, dict) else str(venue)
    relevance_score = event_dict.get("relevance_score", 0.0)
    relevance_reason = event_dict.get("relevance_reason", "")
    matched_keywords = event_dict.get("matched_keywords", [])
    
    # Format date
    date_str = "TBD"
    if start_at:
        try:
            if isinstance(start_at, datetime):
                dt = start_at
            elif isinstance(start_at, str):
                dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            else:
                dt = start_at
            date_str = dt.strftime("%b %d, %Y at %I:%M %p")
        except:
            date_str = str(start_at)
    
    # Build fields
    fields = [
        {
            "type": "mrkdwn",
            "text": f"*Date:*\n{date_str}"
        },
        {
            "type": "mrkdwn",
            "text": f"*Location:*\n{venue_raw[:100]}"
        }
    ]
    
    # Add relevance score
    score_emoji = "🔥" if relevance_score >= 0.7 else "⭐" if relevance_score >= 0.5 else "📌"
    fields.append({
        "type": "mrkdwn",
        "text": f"*Relevance:*\n{score_emoji} {relevance_score:.2f}"
    })
    
    # Build description
    description_parts = []
    if relevance_reason:
        description_parts.append(f"*Why relevant:* {relevance_reason[:200]}")
    if matched_keywords:
        tags = ", ".join(matched_keywords[:5])
        description_parts.append(f"*Topics:* {tags}")
    
    description = "\n".join(description_parts) if description_parts else ""
    
    block = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*<{url}|{title}>*\n{description}"
        },
        "fields": fields
    }
    
    return block


@app.event("app_mention")
def handle_mention(event, say):
    """Handle when the bot is mentioned."""
    global is_scraping
    
    user = event.get("user", "unknown")
    channel = event.get("channel", "unknown")
    logger.info(f"📢 Bot mentioned by user {user} in channel {channel}")
    
    # Check if already scraping
    with scraping_lock:
        if is_scraping:
            logger.warning("⏳ Scrape already in progress, rejecting new request")
            say("⏳ Already scraping events! Please wait...")
            return
        is_scraping = True
        logger.info("✅ Starting new scrape (lock acquired)")
    
    try:
        # Acknowledge the mention
        logger.info("🔍 Sending acknowledgment message to Slack")
        say("🔍 Scraping Luma for relevant events in the SF Bay Area... This may take a minute.")
        
        # Run the scraper with timeout (10 minutes max)
        out_dir: Path = SETTINGS.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"📁 Output directory: {out_dir}")
        
        logger.info("🔑 Building keyword set from Judgment Labs...")
        keywords = build_judgment_keyword_set(SETTINGS.judgment_labs_url)
        logger.info(f"✅ Loaded {len(keywords)} keywords")
        
        def run_scraper():
            logger.info("🚀 Starting scrape_luma_events_with_agent...")
            try:
                result = scrape_luma_events_with_agent(
                    luma_home_url=SETTINGS.luma_home_url,
                    days=SETTINGS.days_ahead,
                    region="sf_bay",
                    sf_terms=SETTINGS.sf_bay_terms,
                    keywords=keywords,
                    out_dir=out_dir,
                    browser_profile_dir=SETTINGS.browser_profile_dir,
                    headless=True,
                    max_events=50,
                )
                logger.info(f"✅ Scraper returned {len(result) if result else 0} events")
                return result
            except Exception as e:
                logger.error(f"❌ Error in run_scraper: {e}", exc_info=True)
                raise
        
        # Run with timeout in a thread
        logger.info("⏱️ Submitting scraper to thread pool (10min timeout)...")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_scraper)
            try:
                events = future.result(timeout=600)  # 10 minute timeout
                logger.info(f"✅ Scraper completed successfully: {len(events) if events else 0} events")
            except FutureTimeoutError:
                logger.error("⏱️ Scraping timed out after 10 minutes")
                say("⏱️ Scraping timed out after 10 minutes. The process may have gotten stuck. Please try again.")
                return
        
        if not events:
            logger.warning("❌ No events found")
            say("❌ No relevant events found in the next 14 days.")
            return
        
        # Format and send events
        logger.info(f"📝 Formatting {len(events)} events for Slack...")
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📅 Found {len(events)} Relevant Events"
                }
            },
            {
                "type": "divider"
            }
        ]
        
        # Add each event
        events_to_show = events[:20]  # Limit to 20 events for Slack message limits
        logger.info(f"📋 Adding {len(events_to_show)} events to message blocks...")
        for event in events_to_show:
            blocks.append(format_event_for_slack(event))
            blocks.append({"type": "divider"})
        
        # If more events, add a note
        if len(events) > 20:
            logger.info(f"ℹ️ {len(events) - 20} additional events not shown (Slack limit)")
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Note:* Showing first 20 of {len(events)} events. Check the full list in the exported files."
                    }
                ]
            })
        
        # Send the formatted message
        logger.info("📤 Sending formatted events to Slack...")
        say(blocks=blocks)
        logger.info("✅ Successfully sent events to Slack!")
        
    except Exception as e:
        logger.error(f"❌ Error in handle_mention: {e}", exc_info=True)
        say(f"❌ Error scraping events: {str(e)}")
    finally:
        with scraping_lock:
            is_scraping = False
            logger.info("🔓 Released scrape lock")


@app.event("message")
def handle_message(event, say):
    """Handle direct messages to the bot."""
    # Only respond to DMs, not channel messages (those use app_mention)
    if event.get("channel_type") == "im":
        logger.info("💬 Received direct message")
        say("👋 Hi! Mention me in a channel to scrape Luma events, or use `/events` command.")


if __name__ == "__main__":
    # Check for required env vars
    logger.info("🔍 Checking environment variables...")
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.error("❌ Missing SLACK_BOT_TOKEN")
        raise SystemExit("Missing env var SLACK_BOT_TOKEN")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("❌ Missing ANTHROPIC_API_KEY")
        raise SystemExit("Missing env var ANTHROPIC_API_KEY")
    if not os.environ.get("SLACK_APP_TOKEN"):
        logger.error("❌ Missing SLACK_APP_TOKEN")
        raise SystemExit("Missing env var SLACK_APP_TOKEN")
    
    logger.info("✅ All environment variables present")
    logger.info("🚀 Starting Slack bot...")
    
    # Start the bot
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    logger.info("🤖 Slack bot is running! Waiting for mentions...")
    handler.start()
