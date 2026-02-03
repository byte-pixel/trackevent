from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from judgeval.tracer import Tracer, wrap
from judgeval.scorers import FaithfulnessScorer, AnswerRelevancyScorer
from judgeval.data import Example

from anthropic import Anthropic
from claude_agent_sdk import query, ClaudeAgentOptions

from src.filtering import is_within_days, looks_like_sf_bay, parse_datetime_loose
from src.models import Event, Organizer, Venue


# Initialize tracer for online monitoring
judgment = Tracer(project_name="luma-scrape-agent")

# Wrap Anthropic client for automatic LLM call tracing
anthropic_client = wrap(Anthropic())

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


@judgment.observe(span_type="tool")
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
async def _run_agent_sdk(days: int, max_events: int, timeout_seconds: int = 180) -> dict[str, Any]:
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

    async def _collect_urls():
        collected_urls: list[str] = []
        final_result = ""
        
        # Use Playwright MCP for browser automation
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                allowed_tools=["WebFetch", "WebSearch"],
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
    
    try:
        return await asyncio.wait_for(_collect_urls(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        print(f"[WARN] URL collection timed out after {timeout_seconds}s, returning partial results")
        return {"event_urls": []}


JUDGMENT_LABS_CONTEXT = """Judgment Labs is an applied research lab focused on:
- Agent behavior monitoring and reliability in production
- LLM evaluation, scoring, and debugging
- Observability and tracing for AI agents
- Anomaly detection and failure pattern analysis
- Production AI safety, security, and privacy (PII detection)
- Agent optimization and performance tuning

Related topics include: AI agents, autonomous agents, LLM ops, ML ops, AI infrastructure, 
model evaluation, prompt engineering, AI safety, AI observability, agent frameworks."""


@judgment.observe(span_type="tool")
async def _extract_event_details(url: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """Use Agent SDK to extract event details from a single Luma page."""
    
    prompt = f"""Fetch {url} and extract the event details.

Return a JSON object with:
- title: event title
- date_text: the event date and time (e.g. "January 25, 2026 6:00 PM" or "Jan 25, 2026"). Look for the date near the top of the page.
- venue_text: location address or "Online" if virtual
- organizer_text: who is hosting the event
- description_text: event description (first 500 chars)

IMPORTANT: Make sure to extract the actual date - look for patterns like "January 25", "Jan 25, 2026", "Tuesday, January 25" etc.

Return ONLY the JSON object, no other text."""

    result = {"url": url, "title": "", "date_text": "", "venue_text": "", "organizer_text": "", "description_text": ""}
    
    async def _extract_inner():
        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    allowed_tools=["WebFetch"],
                )
            ):
                if hasattr(message, "result"):
                    parsed = _extract_json_from_text(message.result)
                    if parsed:
                        result.update(parsed)
                        result["url"] = url
                        return result  # Return early if we got a result
        except Exception as e:
            print(f"[WARN] Failed to extract details from {url}: {e}")
        return result
    
    try:
        # Add timeout to prevent hanging
        result = await asyncio.wait_for(_extract_inner(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        print(f"[WARN] Timeout ({timeout_seconds}s) extracting details from {url}")
    except Exception as e:
        print(f"[WARN] Error extracting details from {url}: {e}")
    
    return result


@judgment.observe(span_type="tool")
def _check_relevance_with_claude(event: dict[str, Any]) -> dict[str, Any]:
    """Use direct Anthropic API to determine if an event is relevant (much faster than Agent SDK)."""
    
    title = event.get("title", "")
    description = event.get("description_text", "")[:800]
    
    prompt = f"""{JUDGMENT_LABS_CONTEXT}

Analyze this event:
Title: {title}
Description: {description}

Is this event relevant to Judgment Labs' field?

Return ONLY a JSON object:
{{"is_relevant": true/false, "relevance_score": 0.0-1.0, "reason": "brief explanation", "matched_topics": ["topic1", "topic2"]}}"""

    result = {"is_relevant": False, "relevance_score": 0.0, "reason": "", "matched_topics": []}
    
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text if response.content else ""
        parsed = _extract_json_from_text(text)
        if parsed:
            result.update(parsed)
    except Exception as e:
        print(f"[WARN] Relevance check failed: {e}")
    
    return result


@judgment.observe(span_type="function")
def _check_relevance_all(events: list[dict]) -> list[dict]:
    """Check relevance for all events using direct API (fast, synchronous)."""
    print(f"[INFO] Checking relevance for {len(events)} events...")
    results = []
    for i, ev in enumerate(events):
        if (i + 1) % 5 == 0:
            print(f"[INFO] Checked {i + 1}/{len(events)} events...")
        results.append(_check_relevance_with_claude(ev))
    print(f"[INFO] Relevance check complete.")
    
    # Online evaluation: monitor relevance decision quality
    relevant_count = sum(1 for r in results if r.get("is_relevant"))
    try:
        sample_decisions = []
        for ev, rel in zip(events[:5], results[:5]):
            title = ev.get("title", "")[:40]
            is_rel = rel.get("is_relevant", False)
            score = rel.get("relevance_score", 0)
            reason = rel.get("reason", "")[:60]
            sample_decisions.append(f"{title}: relevant={is_rel}, score={score:.2f}, reason='{reason}'")
        
        judgment.async_evaluate(
            scorer=faithfulness_scorer,
            example=Example(
                input="Determine which events are relevant to AI agents, monitoring, observability, and LLM evaluation",
                actual_output=f"Found {relevant_count}/{len(results)} relevant events. Sample decisions: {' | '.join(sample_decisions)}",
                retrieval_context=JUDGMENT_LABS_CONTEXT,
            ),
        )
    except Exception as e:
        print(f"[DEBUG] Relevance evaluation skipped: {e}")
    
    return results


@judgment.observe(span_type="function")
async def _extract_all_events_parallel(urls: list[str], batch_size: int = 5, timeout_per_batch: int = 90) -> list[dict]:
    """Extract event details in parallel batches for speed."""
    all_results = []
    total_batches = (len(urls) + batch_size - 1) // batch_size
    
    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]
        batch_num = i//batch_size + 1
        print(f"[INFO] Extracting batch {batch_num}/{total_batches} ({len(batch)} events)...")
        
        async def process_batch():
            # Run batch in parallel with individual timeouts
            tasks = [_extract_event_details(url, timeout_seconds=60) for url in batch]
            return await asyncio.gather(*tasks, return_exceptions=True)
        
        try:
            # Add timeout for the entire batch
            results = await asyncio.wait_for(process_batch(), timeout=timeout_per_batch)
        except asyncio.TimeoutError:
            print(f"[WARN] Batch {batch_num} timed out after {timeout_per_batch}s, skipping remaining events in batch")
            # Create empty results for timed-out batch
            results = [{"url": url, "title": "", "date_text": "", "venue_text": "", "organizer_text": "", "description_text": ""} 
                      for url in batch]
        
        # Process results
        for url, result in zip(batch, results):
            if isinstance(result, Exception):
                print(f"[WARN] Failed to load {url}: {result}")
                all_results.append({"url": url, "title": "", "date_text": "", "venue_text": "", "organizer_text": "", "description_text": ""})
            else:
                all_results.append(result)
        
        print(f"[INFO] Completed batch {batch_num}/{total_batches}")
    
    print(f"[INFO] Finished extracting {len(all_results)} events")
    return all_results


@judgment.observe(span_type="chain")
async def _run_full_pipeline(days: int, max_events: int) -> tuple[list[str], list[dict]]:
    """Run URL collection and event extraction in async context."""
    # Step 1: Collect URLs
    agent_result = await _run_agent_sdk(days=days, max_events=max_events)
    urls = agent_result.get("event_urls", []) or []
    
    # Step 2: Extract details in parallel
    extracted_list = await _extract_all_events_parallel(urls[:max_events], batch_size=5)
    
    return urls, extracted_list


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
    
    # Run the async pipeline (URL collection + extraction)
    urls, extracted_list = asyncio.run(_run_full_pipeline(days=days, max_events=max_events))
    
    print(f"[DEBUG] event_urls count: {len(urls)}")
    if urls:
        print(f"[DEBUG] first 3 URLs: {urls[:3]}")
    print(f"[DEBUG] extracted {len(extracted_list)} event details")
    
    # Run relevance checking synchronously (fast direct API calls)
    relevance_list = _check_relevance_all(extracted_list)
    
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

    # Process extracted details with agent-based relevance
    for idx, (extracted, relevance) in enumerate(zip(extracted_list, relevance_list)):
        url = extracted.get("url", "")
        if not url:
            continue

        # Debug: show what was extracted
        title = (extracted.get("title") or "").strip()
        date_text = extracted.get("date_text") or ""
        venue_raw = extracted.get("venue_text") or ""
        is_relevant = relevance.get("is_relevant", False)
        rel_score = relevance.get("relevance_score", 0.0)
        rel_reason = relevance.get("reason", "")
        matched_topics = relevance.get("matched_topics", [])
        
        print(f"[DEBUG] Event {idx+1}: '{title[:40]}' | relevant={is_relevant} | score={rel_score:.2f}")
        print(f"[DEBUG]   date_text: '{date_text[:60] if date_text else 'EMPTY'}'")
        if rel_reason:
            print(f"[DEBUG]   reason: {rel_reason[:80]}")

        # Parse date - try multiple sources
        dt = parse_datetime_loose(date_text) if date_text else None
        if not dt and extracted.get("description_text"):
            # Try parsing from description if date_text failed
            dt = parse_datetime_loose(extracted.get("description_text", "")[:200])
        
        print(f"[DEBUG]   parsed_date: {dt}")

        ev = Event(
            url=url,
            title=title or url,
            start_at=dt,
            venue=Venue(raw=venue_raw, is_online=("online" in venue_raw.lower() or "virtual" in venue_raw.lower())),
            organizer=Organizer(name=(extracted.get("organizer_text") or "").strip() or None),
            description=(extracted.get("description_text") or "").strip() or None,
            tags=matched_topics,  # Use matched topics as tags
            relevance_reason=rel_reason,  # Why this event was included
            relevance_score=rel_score,
            matched_keywords=matched_topics,
        )

        # Check relevance FIRST (most important filter)
        if not is_relevant or rel_score < 0.3:
            print(f"[DEBUG]   -> FILTERED: not relevant to Judgment Labs")
            continue

        # Date filter - be lenient if we can't parse the date but event is relevant
        if dt is not None and not is_within_days(dt, days=days, now=now):
            print(f"[DEBUG]   -> FILTERED: date {dt} not within {days} days")
            continue
        elif dt is None:
            # Can't parse date - include anyway if relevant (assume it's upcoming)
            print(f"[DEBUG]   -> WARNING: Could not parse date, including anyway (relevant event)")
        
        # Geo filter
        if region == "sf_bay" and not looks_like_sf_bay(venue_raw, sf_terms):
            print(f"[DEBUG]   -> FILTERED: venue not SF Bay")
            continue

        events.append(ev)
        print(f"[INFO] -> MATCHED: {ev.title[:50]}... (score={rel_score:.2f})")

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
    
    # Online evaluation: check if events are relevant using agent-identified topics
    if events:
        try:
            # Collect agent-identified topics and reasons for relevance
            relevance_summaries = []
            for e in events[:5]:
                topics = ", ".join(e.matched_keywords[:3]) if e.matched_keywords else "general AI"
                relevance_summaries.append(
                    f"[{e.title[:40]}] topics: {topics} | score: {e.relevance_score:.2f}"
                )
            
            if relevance_summaries:
                judgment.async_evaluate(
                    scorer=relevancy_scorer,
                    example=Example(
                        input="Find events about AI agents, LLM evaluation, agent monitoring, observability, production reliability, debugging, and related AI infrastructure topics",
                        actual_output=f"Agent-identified relevant events: {' | '.join(relevance_summaries)}",
                    ),
                )
        except Exception as e:
            print(f"[DEBUG] Description relevancy evaluation skipped: {e}")
    
    # Online evaluation: check if relevance reasons are faithful to context
    if events:
        try:
            reasons_with_context = []
            for e in events[:5]:
                if e.relevance_reason:
                    reasons_with_context.append(
                        f"Event: '{e.title[:40]}' | Reason: '{e.relevance_reason}'"
                    )
            
            if reasons_with_context:
                judgment.async_evaluate(
                    scorer=faithfulness_scorer,
                    example=Example(
                        input="Provide accurate reasons why each event is relevant to AI agents, monitoring, and LLM evaluation",
                        actual_output=" | ".join(reasons_with_context),
                        retrieval_context=JUDGMENT_LABS_CONTEXT,
                    ),
                )
        except Exception as e:
            print(f"[DEBUG] Reasons faithfulness evaluation skipped: {e}")
    
    # Final summary evaluation: overall pipeline quality
    if events:
        try:
            summary = f"Pipeline found {len(events)} relevant events from {len(urls)} URLs. "
            summary += f"Top events: " + ", ".join(f"'{e.title[:30]}' (score={e.relevance_score:.2f}, reason='{e.relevance_reason[:50] if e.relevance_reason else 'N/A'}')" for e in events[:3])
            
            judgment.async_evaluate(
                scorer=faithfulness_scorer,
                example=Example(
                    input=f"Find SF Bay Area events in the next {days} days related to Judgment Labs' focus: AI agents, monitoring, observability, LLM evaluation, production reliability",
                    actual_output=summary,
                    retrieval_context=JUDGMENT_LABS_CONTEXT,
                ),
            )
        except Exception as e:
            print(f"[DEBUG] Summary evaluation skipped: {e}")
    
    return events
