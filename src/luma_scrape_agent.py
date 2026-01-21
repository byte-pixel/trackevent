from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from judgeval.tracer import Tracer
from judgeval.scorers import FaithfulnessScorer, AnswerRelevancyScorer
from judgeval.data import Example

from claude_agent_sdk import query, ClaudeAgentOptions, MCPServerConfig

from .filtering import is_within_days, looks_like_sf_bay, parse_datetime_loose, score_relevance
from .models import Event, Organizer, Venue


# Initialize tracer for online monitoring
judgment = Tracer(project_name="luma-scrape-agent")

# Scorers for online evaluation
relevancy_scorer = AnswerRelevancyScorer(threshold=0.6)
faithfulness_scorer = FaithfulnessScorer(threshold=0.7)


def _extract_json_from_text(text: str) -> dict | None:
    """Extract JSON object from text, handling markdown code blocks."""
    # Try extracting from ```json ... ``` block
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding raw JSON object
    match = re.search(r"(\{[\s\S]*\})", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _extract_urls_from_text(text: str) -> list[str]:
    """Extract Luma event URLs from agent output text."""
    urls = re.findall(r'https?://(?:lu\.ma|luma\.com)/[a-zA-Z0-9_-]+', text)
    # Normalize luma.com -> lu.ma
    normalized = []
    for u in urls:
        u = u.replace("https://luma.com/", "https://lu.ma/")
        u = u.replace("http://luma.com/", "https://lu.ma/")
        normalized.append(u)
    return list(dict.fromkeys(normalized))  # dedupe preserving order


@judgment.observe(span_type="chain")
async def _run_agent_sdk(days: int, max_events: int) -> dict[str, Any]:
    """Run the Claude Agent SDK to collect Luma event URLs."""
    
    prompt = f"""Navigate to https://lu.ma/sf to find San Francisco Bay Area events.

Your task:
1. Go to https://lu.ma/sf 
2. Scroll through the page to load more events
3. Extract all event URLs you can find (they look like https://lu.ma/eventid)
4. Collect at least 20-30 event URLs happening in the next {days} days
5. Return a JSON object with the URLs

Return your final answer as a JSON object like this:
{{"event_urls": ["https://lu.ma/abc123", "https://lu.ma/xyz456", ...]}}

Focus on tech/AI related events if you can identify them from titles."""

    collected_urls: list[str] = []
    final_result = ""
    
    # Use Playwright MCP for browser automation
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=["WebFetch", "WebSearch"],
            max_tokens=4000,
        )
    ):
        # Collect output
        if hasattr(message, "result"):
            final_result = message.result
        elif hasattr(message, "content"):
            content = str(message.content)
            # Extract URLs as we go
            urls = _extract_urls_from_text(content)
            collected_urls.extend(urls)
            print(f"[DEBUG] Found {len(urls)} URLs in message")
    
    # Try to parse final result as JSON
    if final_result:
        parsed = _extract_json_from_text(final_result)
        if parsed and "event_urls" in parsed:
            return parsed
        # Also try extracting URLs from final result
        urls = _extract_urls_from_text(final_result)
        collected_urls.extend(urls)
    
    # Dedupe and return
    collected_urls = list(dict.fromkeys(collected_urls))
    return {"event_urls": collected_urls[:max_events]}


@judgment.observe(span_type="tool")
async def _extract_event_details(url: str) -> dict[str, Any]:
    """Use Agent SDK to extract event details from a single Luma page."""
    
    prompt = f"""Fetch {url} and extract the event details.

Return a JSON object with:
- title: event title
- date_text: date and time as shown
- venue_text: location or "Online" if virtual
- organizer_text: who is hosting
- description_text: event description (first 500 chars)

Return ONLY the JSON object, no other text."""

    result = {"url": url, "title": "", "date_text": "", "venue_text": "", "organizer_text": "", "description_text": ""}
    
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                allowed_tools=["WebFetch"],
                max_tokens=2000,
            )
        ):
            if hasattr(message, "result"):
                parsed = _extract_json_from_text(message.result)
                if parsed:
                    result.update(parsed)
                    result["url"] = url
    except Exception as e:
        print(f"[WARN] Failed to extract details from {url}: {e}")
    
    return result


@judgment.observe(span_type="chain")
def scrape_luma_events_with_agent(
    *,
    luma_home_url: str,
    days: int,
    region: str,
    sf_terms: tuple[str, ...],
    keywords: set[str],
    out_dir: Path,
    browser_profile_dir: Path,
    headless: bool,
    max_events: int,
) -> list[Event]:
    """Main entry point - runs the Claude Agent SDK to scrape Luma events."""
    
    # Run the async agent
    agent_result = asyncio.run(_run_agent_sdk(days=days, max_events=max_events))
    
    print(f"[DEBUG] agent_result keys: {list(agent_result.keys())}")
    print(f"[DEBUG] event_urls count: {len(agent_result.get('event_urls', []) or [])}")
    
    urls = agent_result.get("event_urls", []) or []
    if urls:
        print(f"[DEBUG] first 3 URLs: {urls[:3]}")
    
    print(f"[DEBUG] valid URLs after filter: {len(urls)}")
    
    # Online evaluation: only run if we have valid URLs to evaluate
    if urls:
        try:
            urls_preview = ", ".join(urls[:25])
            judgment.async_evaluate(
                scorer=relevancy_scorer,
                example=Example(
                    input=f"Collect SF Bay Area event URLs from Luma for next {days} days",
                    actual_output=f"Successfully collected {len(urls)} Luma event URLs: {urls_preview}",
                ),
            )
        except Exception as e:
            print(f"[DEBUG] Online evaluation skipped: {e}")

    events: list[Event] = []
    now = datetime.now().replace(tzinfo=None)

    # Extract details for each URL
    for i, url in enumerate(urls[:max_events]):
        print(f"[INFO] Extracting event {i+1}/{min(len(urls), max_events)}: {url}")
        
        try:
            extracted = asyncio.run(_extract_event_details(url))
        except Exception as e:
            print(f"[WARN] Failed to load {url}: {e}")
            continue

        # Parse date
        dt = parse_datetime_loose(extracted.get("date_text") or extracted.get("description_text") or "")
        venue_raw = extracted.get("venue_text") or ""

        ev = Event(
            url=url,
            title=(extracted.get("title") or "").strip() or url,
            start_at=dt,
            venue=Venue(raw=venue_raw, is_online=("online" in venue_raw.lower() or "virtual" in venue_raw.lower())),
            organizer=Organizer(name=(extracted.get("organizer_text") or "").strip() or None),
            description=(extracted.get("description_text") or "").strip() or None,
            tags=[],
        )

        # Filters
        if not is_within_days(ev.start_at, days=days, now=now):
            continue
        if region == "sf_bay" and not looks_like_sf_bay(venue_raw, sf_terms):
            continue

        ev = score_relevance(ev, keywords)
        # Require at least some topical signal
        if ev.relevance_score < 1.0:
            continue

        events.append(ev)
        print(f"[INFO] -> Matched: {ev.title[:60]}... (score={ev.relevance_score})")

    events.sort(key=lambda e: (e.start_at or datetime.max, -e.relevance_score))
    
    # Online evaluation: check final event extraction quality
    if events:
        try:
            event_titles = ", ".join(e.title[:50] for e in events[:5])
            keywords_str = ", ".join(list(keywords)[:20])
            judgment.async_evaluate(
                scorer=faithfulness_scorer,
                example=Example(
                    input="Extract events related to AI agents, monitoring, reliability from Luma",
                    actual_output=f"Found events: {event_titles}",
                    retrieval_context=f"Target keywords: {keywords_str}",
                ),
            )
        except Exception as e:
            print(f"[DEBUG] Final evaluation skipped: {e}")
    
    # Online evaluation: check if event descriptions are relevant to AI/agents domain
    if events:
        try:
            # Extract sentences containing matched keywords for better evaluation context
            keyword_sentences = []
            for e in events[:5]:
                if not e.description or not e.matched_keywords:
                    continue
                # Split into sentences and find ones with keyword hits
                sentences = re.split(r'[.!?]+', e.description)
                for sentence in sentences:
                    sentence = sentence.strip()
                    if len(sentence) < 20:
                        continue
                    sentence_lower = sentence.lower()
                    matching_kws = [kw for kw in e.matched_keywords if kw.lower() in sentence_lower]
                    if matching_kws:
                        keyword_sentences.append(
                            f"[{e.title[:30]}] ({', '.join(matching_kws[:3])}): {sentence[:200]}"
                        )
                        if len(keyword_sentences) >= 8:
                            break
                if len(keyword_sentences) >= 8:
                    break
            
            if keyword_sentences:
                judgment.async_evaluate(
                    scorer=relevancy_scorer,
                    example=Example(
                        input="Find events about AI agents, LLM evaluation, agent monitoring, observability, production reliability, debugging, and related AI infrastructure topics",
                        actual_output=f"Sentences with keyword matches: {' | '.join(keyword_sentences)}",
                    ),
                )
        except Exception as e:
            print(f"[DEBUG] Description relevancy evaluation skipped: {e}")
    
    return events
