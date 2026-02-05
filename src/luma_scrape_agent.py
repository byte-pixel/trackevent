from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from judgeval.tracer import Tracer, wrap
from judgeval.scorers import FaithfulnessScorer, AnswerRelevancyScorer
from judgeval.data import Example

from anthropic import Anthropic
import httpx
from bs4 import BeautifulSoup

from src.filtering import is_within_days, looks_like_sf_bay, parse_datetime_loose
from src.models import Event, Organizer, Venue

# Set up logging (will inherit from parent if already configured)
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stdout,
        force=True
    )


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
    """Extract Luma event URLs from text, filtering out user profiles and JSON property names."""
    # Extract all potential URLs from lu.ma
    # Pattern 1: Full URLs
    urls = re.findall(r'https?://(?:lu\.ma|luma\.com)/([a-zA-Z0-9_-]+)', text)
    # Pattern 2: URLs in quotes/JSON
    urls.extend(re.findall(r'["\']https?://(?:lu\.ma|luma\.com)/([a-zA-Z0-9_-]+)["\']', text))
    # Pattern 3: Relative URLs in href attributes
    urls.extend(re.findall(r'href=["\']/?([a-zA-Z0-9_-]+)["\']', text))
    
    # Filter out JSON property names and invalid patterns
    excluded = {
        'sf', 'ios', 'android', 'web', 'about', 'help', 'privacy', 'terms', 
        'login', 'signup', 'explore', 'discover', 'events', 'organizers',
        'venues', 'contact', 'blog', 'jobs', 'press', 'api', 'docs',
        'create', 'event', 'description', 'slug', 'url', 'image', 'info',
        'hero_image_mobile_url', 'hero_image_desktop_url', 'is_free',
        'virtual_info', 'personal_user', 'create', 'event', 'description'
    }
    
    # Patterns that indicate JSON property names (not URLs)
    json_patterns = [
        r'^[a-z_]+$',  # snake_case (like hero_image_mobile_url)
        r'^[a-z]+[A-Z]',  # camelCase
    ]
    
    # Patterns that indicate non-event URLs
    non_event_patterns = [
        r'^usr-',  # User profiles
        r'^cal-',  # Calendars
        r'^org-',  # Organizations
    ]
    
    normalized = []
    for url_id in urls:
        url_id_clean = url_id.strip('/').split('/')[0].split('?')[0]
        
        # Skip if in excluded list
        if url_id_clean.lower() in excluded:
            continue
        
        # Skip if it looks like a JSON property name
        if any(re.match(pattern, url_id_clean) for pattern in json_patterns):
            continue
        
        # Skip if it's a non-event URL pattern (user profiles, calendars, etc.)
        if any(re.match(pattern, url_id_clean) for pattern in non_event_patterns):
            continue
        
        # Skip if it's too short (likely not a valid event ID)
        # Reduced from 6 to 4 to catch more events
        if len(url_id_clean) < 4:
            continue
        
        # Skip if it's all lowercase and very short (likely a page path, not an event ID)
        # But be more lenient - allow longer lowercase strings
        if url_id_clean.islower() and len(url_id_clean) < 8:
            # But allow if it starts with 'evt-' (known event prefix)
            if not url_id_clean.startswith('evt-'):
                continue
        
        # Include valid-looking event URLs
        normalized.append(f"https://lu.ma/{url_id_clean}")
    
    return list(dict.fromkeys(normalized))  # dedupe preserving order


@judgment.observe(span_type="chain")
async def _run_agent_sdk(days: int, max_events: int, timeout_seconds: int = 180) -> dict[str, Any]:
    """Collect Luma event URLs using Playwright to render JavaScript-loaded content."""
    
    logger.info("🌐 Using Playwright to fetch Luma SF page (rendering JavaScript)...")
    all_urls = set()
    html = None
    
    try:
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            # Launch browser in headless mode (memory efficient)
            browser = await p.chromium.launch(headless=True)
            try:
                # Create a new page
                page = await browser.new_page()
                
                # Try multiple Luma pages to get more events
                pages_to_try = [
                    "https://lu.ma/sf",
                    "https://lu.ma/sf/events",
                    "https://lu.ma/explore/sf",
                ]
                
                html = None
                for page_url in pages_to_try:
                    try:
                        logger.info(f"🌐 Loading {page_url} with Playwright...")
                        # Navigate and wait for content to load
                        await page.goto(page_url, wait_until="networkidle", timeout=30000)
                        # Wait for JavaScript to render events
                        await page.wait_for_timeout(3000)
                        
                        # Scroll down to load more events (Luma uses infinite scroll)
                        logger.info("📜 Scrolling to load more events...")
                        for scroll in range(5):  # Scroll 5 times
                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            await page.wait_for_timeout(2000)  # Wait 2s between scrolls
                            # Check if we've reached the bottom
                            is_at_bottom = await page.evaluate("""
                                window.innerHeight + window.scrollY >= document.body.scrollHeight - 100
                            """)
                            if is_at_bottom:
                                logger.info(f"✅ Reached bottom after {scroll + 1} scrolls")
                                break
                        
                        # Wait a bit more for any lazy-loaded content
                        await page.wait_for_timeout(2000)
                        html = await page.content()
                        logger.info(f"✅ Rendered {page_url}: {len(html)} chars")
                        break  # Use first successful page
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to load {page_url}: {e}")
                        continue
                
                if not html:
                    # Fallback: try default page
                    logger.warning("⚠️ All pages failed, trying default...")
                    await page.goto("https://lu.ma/sf", wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(3000)
                    # Scroll to load more
                    for scroll in range(5):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(2000)
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    logger.info(f"✅ Rendered default page: {len(html)} chars")
                
                # Close page immediately to save memory
                await page.close()
            finally:
                # Always close browser to free memory
                await browser.close()
            
    except ImportError:
        logger.warning("⚠️ Playwright not available, falling back to HTTP requests...")
        # Fallback to HTTP requests if Playwright is not installed
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get("https://lu.ma/sf", follow_redirects=True)
                response.raise_for_status()
                html = response.text
                logger.info(f"✅ Fetched via HTTP: {len(html)} chars")
        except Exception as e:
            logger.error(f"❌ HTTP fallback also failed: {e}")
            return {"event_urls": []}
    except Exception as e:
        logger.error(f"❌ Error fetching Luma page with Playwright: {e}", exc_info=True)
        # Try HTTP fallback
        try:
            logger.info("🔄 Attempting HTTP fallback...")
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get("https://lu.ma/sf", follow_redirects=True)
                response.raise_for_status()
                html = response.text
                logger.info(f"✅ Fetched via HTTP fallback: {len(html)} chars")
        except Exception as e2:
            logger.error(f"❌ HTTP fallback also failed: {e2}")
            return {"event_urls": []}
    
    # If we have HTML (from Playwright or HTTP fallback), extract URLs
    if html:
        # Method 1: Extract URLs from HTML using regex
        urls_regex = _extract_urls_from_text(html)
        all_urls.update(urls_regex)
        logger.info(f"🔗 Found {len(urls_regex)} URLs via regex")
        
        # Method 2: Parse HTML with BeautifulSoup to find href attributes
        soup = None
        try:
            soup = BeautifulSoup(html, 'lxml')
            # Find all links that point to lu.ma events
            links = soup.find_all('a', href=True)
            excluded = {'sf', 'ios', 'android', 'web', 'about', 'help', 'privacy', 'terms', 
                       'login', 'signup', 'explore', 'discover', 'events', 'organizers',
                       'venues', 'contact', 'blog', 'jobs', 'press', 'api', 'docs',
                       'create', 'event', 'description', 'slug', 'url', 'image', 'info'}
            for link in links:
                href = link.get('href', '')
                if 'lu.ma' in href or 'luma.com' in href:
                    # Make absolute URL
                    if href.startswith('/'):
                        href = 'https://lu.ma' + href
                    elif not href.startswith('http'):
                        href = 'https://lu.ma/' + href
                    # Normalize
                    href = href.replace('luma.com', 'lu.ma')
                    if href.startswith('https://lu.ma/'):
                        # Extract the ID part
                        url_id = href.replace('https://lu.ma/', '').split('/')[0].split('?')[0]
                        # Filter out user profiles (usr-), calendars (cal-), orgs (org-), and excluded pages
                        if url_id.lower() in excluded:
                            continue
                        if url_id.startswith(('usr-', 'cal-', 'org-')):
                            continue
                            # Skip if too short (likely not an event) - reduced from 6 to 4
                            if len(url_id) < 4:
                                continue
                            # Skip if it's all lowercase and very short (likely a page path)
                            # Be more lenient - allow longer lowercase strings
                            if url_id.islower() and len(url_id) < 8 and not url_id.startswith('evt-'):
                                continue
                        # Include valid-looking event URLs
                        all_urls.add(href)
            logger.info(f"🔗 Found {len(all_urls)} total URLs after parsing HTML")
        except Exception as e:
            logger.warning(f"⚠️ Error parsing HTML: {e}")
        
        # Method 3: Look for JSON data in script tags
        if soup:
            try:
                scripts = soup.find_all('script')
                excluded = {'sf', 'ios', 'android', 'web', 'about', 'help', 'privacy', 'terms', 
                           'login', 'signup', 'explore', 'discover', 'events', 'organizers',
                           'venues', 'contact', 'blog', 'jobs', 'press', 'api', 'docs',
                           'create', 'event', 'description', 'slug', 'url', 'image', 'info',
                           'hero_image_mobile_url', 'hero_image_desktop_url', 'is_free',
                           'virtual_info', 'personal_user'}
                json_patterns = [
                    r'^[a-z_]+$',  # snake_case
                    r'^[a-z]+[A-Z]',  # camelCase
                ]
                for script in scripts:
                    if script.string:
                        # Extract all lu.ma URLs from scripts
                        script_urls = re.findall(r'https?://(?:lu\.ma|luma\.com)/([a-zA-Z0-9_-]+)', script.string)
                        for url_id in script_urls:
                            # Skip excluded and non-event patterns
                            if url_id.lower() in excluded:
                                continue
                            if url_id.startswith(('usr-', 'cal-', 'org-')):
                                continue
                            # Skip JSON property names
                            if any(re.match(pattern, url_id) for pattern in json_patterns):
                                continue
                            # Skip if too short - reduced from 6 to 4 to catch more events
                            if len(url_id) < 4:
                                continue
                            # Skip if all lowercase and very short (unless it's evt-)
                            # Be more lenient - allow longer lowercase strings
                            if url_id.islower() and len(url_id) < 8 and not url_id.startswith('evt-'):
                                continue
                            all_urls.add(f"https://lu.ma/{url_id}")
                logger.info(f"🔗 Found {len(all_urls)} total URLs after checking scripts")
            except Exception as e:
                logger.warning(f"⚠️ Error parsing scripts: {e}")
        
        # Method 4: Look for data attributes
        if soup:
            try:
                excluded = {'sf', 'ios', 'android', 'web', 'about', 'help', 'privacy', 'terms', 
                           'login', 'signup', 'explore', 'discover', 'events', 'organizers',
                           'venues', 'contact', 'blog', 'jobs', 'press', 'api', 'docs',
                           'create', 'event', 'description', 'slug', 'url', 'image', 'info'}
                json_patterns = [
                    r'^[a-z_]+$',  # snake_case
                    r'^[a-z]+[A-Z]',  # camelCase
                ]
                # Find elements with data-event-id or similar attributes
                for elem in soup.find_all(attrs=lambda x: x and any(k.startswith('data-') for k in x.keys())):
                    for attr, value in elem.attrs.items():
                        if attr.startswith('data-') and isinstance(value, str):
                            # Skip excluded and non-event patterns
                            if value.lower() in excluded:
                                continue
                            if value.startswith(('usr-', 'cal-', 'org-')):
                                continue
                            # Skip JSON property names
                            if any(re.match(pattern, value) for pattern in json_patterns):
                                continue
                            # Skip if too short - reduced from 6 to 4 to catch more events
                            if len(value) < 4:
                                continue
                            # Skip if all lowercase and very short (unless it's evt-)
                            # Be more lenient - allow longer lowercase strings
                            if value.islower() and len(value) < 8 and not value.startswith('evt-'):
                                continue
                            all_urls.add(f"https://lu.ma/{value}")
                logger.info(f"🔗 Found {len(all_urls)} total URLs after checking data attributes")
            except Exception as e:
                logger.warning(f"⚠️ Error parsing data attributes: {e}")
        
        # Convert to list and dedupe
        urls = list(all_urls)
        logger.info(f"✅ Total unique URLs found: {len(urls)}")
        if urls:
            logger.info(f"📋 Sample URLs: {urls[:5]}")
        
        # If we have URLs, return them
        if urls:
            logger.info(f"📋 Returning {min(len(urls), max_events)} URLs (will filter by relevance later)")
            return {"event_urls": urls[:max_events]}
        else:
            logger.warning("⚠️ No URLs found in HTML")
            return {"event_urls": []}
    else:
        logger.error("❌ No HTML content retrieved")
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
    """Extract event details using direct HTTP + Anthropic API (no Agent SDK)."""
    
    result = {"url": url, "title": "", "date_text": "", "venue_text": "", "organizer_text": "", "description_text": ""}
    
    try:
        logger.debug(f"🔍 Fetching event page: {url}")
        
        # Fetch the event page directly
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            html = response.text
            logger.debug(f"✅ Fetched {len(html)} chars from {url}")
            
            # Use BeautifulSoup to parse HTML
            soup = BeautifulSoup(html, 'lxml')
            
            # Method 1: Look for structured data (JSON-LD, microdata)
            date_candidates = []
            
            # Check for JSON-LD structured data
            json_ld_scripts = soup.find_all('script', type='application/ld+json')
            for script in json_ld_scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        # Look for startDate in event schema
                        if data.get('@type') == 'Event' and data.get('startDate'):
                            date_candidates.append(data['startDate'])
                        # Also check if it's a list
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and item.get('@type') == 'Event' and item.get('startDate'):
                                date_candidates.append(item['startDate'])
                except:
                    pass
            
            # Method 2: Look for meta tags with dates
            meta_tags = soup.find_all('meta')
            for meta in meta_tags:
                prop = meta.get('property', '') or meta.get('name', '')
                content = meta.get('content', '')
                if any(keyword in prop.lower() for keyword in ['date', 'time', 'event', 'start']):
                    if content and len(content) > 5:
                        date_candidates.append(content)
            
            # Method 3: Search HTML for date patterns before removing scripts
            date_patterns = [
                r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}',
                r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}',
                r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}',
                r'\d{1,2}/\d{1,2}/\d{4}',
                r'\d{4}-\d{2}-\d{2}',
                r'\b\d{1,2}:\d{2}\s*(AM|PM|am|pm)',
            ]
            
            # Search in visible text and attributes
            all_text = soup.get_text(separator=' ', strip=True)
            for pattern in date_patterns:
                matches = re.findall(pattern, all_text, re.IGNORECASE)
                if matches:
                    # Take first few matches
                    date_candidates.extend([m if isinstance(m, str) else ' '.join(m) for m in matches[:3]])
            
            # Also search in data attributes and class names that might contain dates
            for elem in soup.find_all(attrs=True):
                for attr_name, attr_value in elem.attrs.items():
                    if isinstance(attr_value, str) and any(keyword in attr_name.lower() for keyword in ['date', 'time']):
                        if len(attr_value) > 5 and len(attr_value) < 50:
                            date_candidates.append(attr_value)
            
            # Remove duplicates and clean up
            date_candidates = list(dict.fromkeys(date_candidates))[:10]  # Keep first 10 unique
            
            # Remove script and style elements for text extraction
            for script in soup(["script", "style"]):
                script.decompose()
            text_content = soup.get_text(separator=' ', strip=True)
            # Increase limit to include more context for date finding
            text_content = text_content[:10000]
            
            # Build date hint for Claude
            date_hint = ""
            if date_candidates:
                date_hint = f"\n\nPOTENTIAL DATES FOUND IN PAGE: {', '.join(date_candidates[:5])}\nUse these as hints to find the actual event date/time."
            
            # Use Anthropic API directly to extract structured data
            prompt = f"""Extract event details from this Luma event page HTML content:

{text_content}{date_hint}

Return a JSON object with:
- title: event title
- date_text: the event date and time in a clear, complete format (e.g. "January 25, 2026 6:00 PM" or "Jan 25, 2026 at 6:00 PM" or "Tuesday, January 25, 2026")
- venue_text: location address or "Online" if virtual
- organizer_text: who is hosting the event
- description_text: event description (first 500 chars)

CRITICAL: The date is very important! Look carefully for:
- Full dates like "January 25, 2026" or "Jan 25, 2026"
- Dates with times like "January 25, 2026 at 6:00 PM" or "Jan 25, 2026 6:00 PM"
- Day names like "Tuesday, January 25, 2026"
- ISO dates like "2026-01-25"
- Times like "6:00 PM" or "18:00"
- Any date/time information in the page

If you see potential dates listed above, use them as hints to find the actual event date.
The date might be in various formats - extract it in a clear, readable format.

Return ONLY the JSON object, no other text."""

            logger.debug(f"🤖 Using Anthropic API to extract details from {url}...")
            api_response = anthropic_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            
            result_text = api_response.content[0].text if api_response.content else ""
            parsed = _extract_json_from_text(result_text)
            if parsed:
                result.update(parsed)
                result["url"] = url
                logger.debug(f"✅ Extracted details for {url}: {result.get('title', 'no title')[:50]}")
                if date_candidates and not result.get("date_text"):
                    logger.debug(f"💡 Found {len(date_candidates)} date candidates but Claude didn't extract date")
            else:
                logger.warning(f"⚠️ Could not parse JSON from API response for {url}")
                
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ Timeout ({timeout_seconds}s) extracting details from {url}")
    except Exception as e:
        logger.warning(f"❌ Error extracting details from {url}: {e}")
    
    return result


@judgment.observe(span_type="tool")
def _check_relevance_with_claude(event: dict[str, Any]) -> dict[str, Any]:
    """Use direct Anthropic API to determine if an event is relevant (much faster than Agent SDK)."""
    
    title = event.get("title") or ""
    description_raw = event.get("description_text")
    description = (description_raw[:800] if description_raw else "") or ""
    
    prompt = f"""{JUDGMENT_LABS_CONTEXT}

Analyze this event:
Title: {title}
Description: {description}

Is this event highly relevant to Judgment Labs' field? Be strict - only mark as relevant if the event is directly related to:
- AI agent monitoring, observability, or reliability
- LLM evaluation, scoring, or debugging
- Production AI safety or agent optimization
- Agent frameworks or AI infrastructure

Return ONLY a JSON object:
{{"is_relevant": true/false, "relevance_score": 0.0-1.0, "reason": "brief explanation", "matched_topics": ["topic1", "topic2"]}}

Use a strict scoring scale:
- 0.7-1.0: Highly relevant, directly related to Judgment Labs' core focus
- 0.5-0.7: Moderately relevant, tangentially related
- 0.0-0.5: Not relevant or only loosely related"""

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
    logger.info(f"🔍 Checking relevance for {len(events)} events...")
    results = []
    for i, ev in enumerate(events):
        if (i + 1) % 5 == 0:
            logger.info(f"✅ Checked {i + 1}/{len(events)} events...")
        results.append(_check_relevance_with_claude(ev))
    logger.info(f"✅ Relevance check complete.")
    
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
async def _extract_all_events_parallel(urls: list[str], batch_size: int = 2, timeout_per_batch: int = 90) -> list[dict]:
    """Extract event details in parallel batches for speed.
    
    Reduced batch_size from 5 to 2 to save memory on cloud deployments.
    """
    all_results = []
    total_batches = (len(urls) + batch_size - 1) // batch_size
    
    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]
        batch_num = i//batch_size + 1
        logger.info(f"📦 Extracting batch {batch_num}/{total_batches} ({len(batch)} events)...")
        
        async def process_batch():
            # Run batch in parallel with individual timeouts
            tasks = [_extract_event_details(url, timeout_seconds=60) for url in batch]
            return await asyncio.gather(*tasks, return_exceptions=True)
        
        try:
            # Add timeout for the entire batch
            results = await asyncio.wait_for(process_batch(), timeout=timeout_per_batch)
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ Batch {batch_num} timed out after {timeout_per_batch}s, skipping remaining events in batch")
            # Create empty results for timed-out batch
            results = [{"url": url, "title": "", "date_text": "", "venue_text": "", "organizer_text": "", "description_text": ""} 
                      for url in batch]
        
        # Process results
        for url, result in zip(batch, results):
            if isinstance(result, Exception):
                logger.warning(f"⚠️ Failed to load {url}: {result}")
                all_results.append({"url": url, "title": "", "date_text": "", "venue_text": "", "organizer_text": "", "description_text": ""})
            else:
                all_results.append(result)
        
        logger.info(f"✅ Completed batch {batch_num}/{total_batches}")
    
    logger.info(f"✅ Finished extracting {len(all_results)} events")
    return all_results


@judgment.observe(span_type="chain")
async def _run_full_pipeline(days: int, max_events: int) -> tuple[list[str], list[dict]]:
    """Run URL collection and event extraction in async context."""
    logger.info(f"🚀 Starting full pipeline: days={days}, max_events={max_events}")
    
    # Step 1: Collect URLs
    logger.info("📍 Step 1: Collecting event URLs...")
    agent_result = await _run_agent_sdk(days=days, max_events=max_events)
    urls = agent_result.get("event_urls", []) or []
    logger.info(f"✅ Step 1 complete: Collected {len(urls)} URLs")
    
    # Step 2: Extract details in parallel (reduced batch size for memory efficiency)
    logger.info(f"📍 Step 2: Extracting details from {len(urls[:max_events])} URLs...")
    extracted_list = await _extract_all_events_parallel(urls[:max_events], batch_size=2)
    logger.info(f"✅ Step 2 complete: Extracted {len(extracted_list)} event details")
    
    logger.info("✅ Full pipeline complete!")
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
    
    logger.info(f"🎯 Starting scrape_luma_events_with_agent: days={days}, max_events={max_events}")
    
    # Run the async pipeline (URL collection + extraction)
    logger.info("🔄 Running async pipeline...")
    urls, extracted_list = asyncio.run(_run_full_pipeline(days=days, max_events=max_events))
    
    logger.info(f"📊 Pipeline results: {len(urls)} URLs, {len(extracted_list)} extracted details")
    if urls:
        logger.info(f"🔗 First 3 URLs: {urls[:3]}")
    
    # Run relevance checking synchronously (fast direct API calls)
    relevance_list = _check_relevance_all(extracted_list)
    
    # Initialize events list (must be outside conditional)
    events: list[Event] = []
    now = datetime.now().replace(tzinfo=None)

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

    # Process extracted details with agent-based relevance
    for idx, (extracted, relevance) in enumerate(zip(extracted_list, relevance_list)):
        url = extracted.get("url", "")
        if not url:
            continue

        # Debug: show what was extracted
        title = (extracted.get("title") or "").strip()
        date_text = extracted.get("date_text") or ""
        venue_raw = extracted.get("venue_text") or ""
        description = (extracted.get("description_text") or "").strip()
        is_relevant = relevance.get("is_relevant", False)
        rel_score = relevance.get("relevance_score", 0.0)
        rel_reason = relevance.get("reason", "")
        matched_topics = relevance.get("matched_topics", [])
        
        # Skip events with no title or description (invalid/incomplete extraction)
        if not title and not description:
            print(f"[DEBUG] Event {idx+1}: SKIPPED - No title or description (invalid event)")
            continue
        
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

        # Check relevance FIRST (most important filter) - tightened threshold
        # Increased from 0.3 to 0.5 to be more selective
        if not is_relevant or rel_score < 0.5:
            print(f"[DEBUG]   -> FILTERED: not relevant to Judgment Labs (score={rel_score:.2f} < 0.5)")
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

    # Sort by relevance score (highest first), then by date
    events.sort(key=lambda e: (-e.relevance_score, e.start_at or datetime.max))
    
    # Limit to top 7 events (tightened criteria)
    max_events_to_return = 7
    if len(events) > max_events_to_return:
        logger.info(f"📊 Limiting results from {len(events)} to top {max_events_to_return} events by relevance score")
        events = events[:max_events_to_return]
    
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
