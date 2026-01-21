from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from judgeval.tracer import Tracer, wrap
from judgeval.scorers import FaithfulnessScorer, AnswerRelevancyScorer
from judgeval.data import Example

from anthropic import Anthropic

from .browser_tools import PlaywrightBrowserTools, tool_schemas
from .filtering import is_within_days, looks_like_sf_bay, parse_datetime_loose, score_relevance
from .models import Event, Organizer, Venue


# Initialize tracer for online monitoring
judgment = Tracer(project_name="luma-scrape-agent")
traced_client = wrap(Anthropic())

# Scorers for online evaluation
relevancy_scorer = AnswerRelevancyScorer(threshold=0.6)
faithfulness_scorer = FaithfulnessScorer(threshold=0.7)

@judgment.observe(span_type="tool")
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


SYSTEM_PROMPT = """You are a browser automation agent that collects Luma event URLs.

WORKFLOW:
1. Navigate to https://lu.ma/sf or search for "San Francisco" events
2. Scroll and use extract_dom to collect event links (look for href containing /e/ or lu.ma/)
3. Once you have 15-30 event URLs, STOP collecting and output results immediately

CRITICAL: After collecting URLs, you MUST output a JSON object (not in a code block) like:
{"event_urls": ["https://lu.ma/abc123", "https://lu.ma/xyz456", ...], "per_event": []}

Do NOT visit individual event pages. Just collect the URLs from listing pages.
Do NOT wrap JSON in markdown code blocks.
Stop and return results once you have collected 15+ URLs or after 25 tool calls.
"""


@judgment.observe(span_type="tool")
def _tool_dispatch(browser: PlaywrightBrowserTools, name: str, inp: dict) -> dict:
    if name == "navigate":
        return browser.navigate(inp["url"])
    if name == "content_snapshot":
        return browser.content_snapshot(inp.get("max_chars", 12000))
    if name == "click":
        return browser.click(inp["selector"])
    if name == "click_text":
        return browser.click_text(inp["text"])
    if name == "type":
        return browser.type(inp["selector"], inp["text"])
    if name == "press":
        return browser.press(inp["selector"], inp["key"])
    if name == "scroll":
        return browser.scroll(inp.get("dy", 1200))
    if name == "wait_for":
        return browser.wait_for(inp["selector"], inp.get("timeout_ms", 10000))
    if name == "extract_dom":
        return browser.extract_dom(inp["selectors"])
    if name == "extract_text":
        return browser.extract_text(inp["selector"])
    if name == "screenshot":
        return browser.screenshot(inp["path"])
    raise ValueError(f"Unknown tool: {name}")

@judgment.observe(span_type="function")
def _run_agent(
    *,
    anthropic_client,
    browser: PlaywrightBrowserTools,
    days: int,
    max_events: int,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Collect SF Bay Area event URLs from Luma. Target: 20-30 URLs. "
                f"Use extract_dom with selectors like 'a[href*=\"lu.ma\"]' or 'a' to find event links. "
                "Once you have 15+ URLs, immediately output the final JSON (not in code blocks). "
                "Do NOT visit individual events - just collect URLs from listing/calendar pages."
            ),
        }
    ]

    tools = tool_schemas()

    # Navigate to SF events page directly
    browser.navigate("https://lu.ma/sf")
    snap = browser.content_snapshot()
    messages.append(
        {
            "role": "user",
            "content": (
                "I've navigated to https://lu.ma/sf. Here's the page content:\n"
                f"{json.dumps(snap)}\n\n"
                "Use extract_dom and scroll to collect event URLs, then output final JSON."
            ),
        }
    )

    for _step in range(60):
        resp = anthropic_client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            system=SYSTEM_PROMPT,
            max_tokens=4000,
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": resp.content})

        tool_used = False
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_used = True
            print(f"[DEBUG] step {_step}: tool={block.name}, input={str(block.input)[:100]}")
            try:
                out = _tool_dispatch(browser, block.name, block.input)
            except Exception as e:
                # Return error to agent so it can try a different approach
                out = {"error": str(e)[:300]}
                print(f"[DEBUG] tool error: {out['error'][:100]}")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(out),
                        }
                    ],
                }
            )

        if not tool_used:
            # Attempt to parse the assistant's text as JSON
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
            text = (text or "").strip()
            print(f"[DEBUG] step {_step}: no tool used, text length={len(text)}")
            if text:
                print(f"[DEBUG] text preview: {text[:500]}...")
            result = _extract_json_from_text(text)
            if result and "event_urls" in result:
                print(f"[DEBUG] parsed JSON with keys: {list(result.keys())}")
                return result
            time.sleep(0.2)

        time.sleep(0.2)

    print("[DEBUG] exhausted 60 steps without final result")
    return {"event_urls": [], "per_event": []}


@judgment.observe(span_type="tool")
def _best_effort_extract_event(browser: PlaywrightBrowserTools) -> dict[str, Any]:
    """Fast extraction using just page snapshot - no slow selector lookups."""
    snap = browser.content_snapshot(max_chars=8000)
    page_text = snap.get("text", "")
    
    # Title from page title or first line
    title = snap.get("title", "")
    
    # Look for date patterns in text
    date_text = ""
    import re
    date_match = re.search(
        r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)?",
        page_text, re.IGNORECASE
    )
    if date_match:
        date_text = date_match.group(0)

    return {
        "url": snap.get("url"),
        "title": title,
        "date_text": date_text,
        "venue_text": page_text[:500],  # First part often has location
        "organizer_text": "",
        "description_text": page_text[:4000],
    }


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
    browser = PlaywrightBrowserTools(profile_dir=browser_profile_dir, headless=headless)
    browser.start()
    try:
        agent_result = _run_agent(anthropic_client=traced_client, browser=browser, days=days, max_events=max_events)
        print(f"[DEBUG] agent_result keys: {list(agent_result.keys())}")
        print(f"[DEBUG] event_urls count: {len(agent_result.get('event_urls', []) or [])}")
        if agent_result.get("event_urls"):
            print(f"[DEBUG] first 3 URLs: {agent_result['event_urls'][:3]}")
        
        # Normalize and dedupe URLs first
        urls: list[str] = []
        for u in agent_result.get("event_urls", []) or []:
            if isinstance(u, str) and u.startswith("http"):
                # Normalize luma.com -> lu.ma
                u = u.replace("https://luma.com/", "https://lu.ma/")
                u = u.replace("http://luma.com/", "https://lu.ma/")
                urls.append(u)
        urls = list(dict.fromkeys(urls))  # dedupe preserving order
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

        for i, url in enumerate(urls[:max_events]):
            print(f"[INFO] Extracting event {i+1}/{min(len(urls), max_events)}: {url}")
            try:
                browser.navigate(url)
            except Exception as e:
                print(f"[WARN] Failed to load {url}: {e}")
                continue
            extracted = _best_effort_extract_event(browser)

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
        
        return events
    finally:
        browser.stop()

