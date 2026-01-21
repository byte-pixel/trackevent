from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright


@dataclass
class BrowserState:
    playwright: Any
    context: BrowserContext
    page: Page


class PlaywrightBrowserTools:
    """
    Minimal, deterministic tool surface for an LLM/agent.

    We intentionally keep the API small and text-based so selectors are robust.
    """

    def __init__(self, *, profile_dir: Path, headless: bool):
        self._profile_dir = profile_dir
        self._headless = headless
        self._state: Optional[BrowserState] = None

    def start(self) -> None:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=self._headless,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        self._state = BrowserState(playwright=pw, context=context, page=page)

    def stop(self) -> None:
        if not self._state:
            return
        try:
            self._state.context.close()
        finally:
            self._state.playwright.stop()
            self._state = None

    @property
    def page(self) -> Page:
        if not self._state:
            raise RuntimeError("Browser not started")
        return self._state.page

    # ----- Tools -----

    def navigate(self, url: str) -> dict:
        self.page.goto(url, wait_until="domcontentloaded")
        return {"url": self.page.url, "title": self.page.title()}

    def wait_for(self, selector: str, timeout_ms: int = 10000) -> dict:
        self.page.wait_for_selector(selector, timeout=timeout_ms)
        return {"ok": True}

    def click(self, selector: str) -> dict:
        self.page.locator(selector).first.click()
        return {"ok": True}

    def click_text(self, text: str, timeout_ms: int = 5000) -> dict:
        # Try common clickable roles with shorter timeout
        # Use partial/substring match for long text
        short = text[:60] if len(text) > 60 else text
        loc = self.page.get_by_role("link", name=short)
        if loc.count() == 0:
            loc = self.page.get_by_role("button", name=short)
        if loc.count() == 0:
            loc = self.page.locator(f"text={json.dumps(short)}")
        loc.first.click(timeout=timeout_ms)
        return {"ok": True}

    def type(self, selector: str, text: str) -> dict:
        self.page.locator(selector).first.fill(text)
        return {"ok": True}

    def press(self, selector: str, key: str) -> dict:
        self.page.locator(selector).first.press(key)
        return {"ok": True}

    def scroll(self, dy: int = 1200) -> dict:
        self.page.mouse.wheel(0, dy)
        return {"ok": True}

    def extract_text(self, selector: str) -> dict:
        el = self.page.locator(selector).first
        return {"text": (el.inner_text() or "").strip()}

    def extract_dom(self, selectors: list[str]) -> dict:
        out: dict[str, Any] = {}
        for sel in selectors:
            nodes = self.page.locator(sel)
            items: list[dict[str, Any]] = []
            for i in range(min(nodes.count(), 40)):
                n = nodes.nth(i)
                items.append(
                    {
                        "text": (n.inner_text() or "").strip(),
                        "href": n.get_attribute("href"),
                    }
                )
            out[sel] = items
        return {"selectors": out, "url": self.page.url, "title": self.page.title()}

    def content_snapshot(self, max_chars: int = 12000) -> dict:
        # Text-only snapshot for LLM reasoning.
        body = self.page.locator("body").inner_text() or ""
        body = " ".join(body.split())
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "text": body[:max_chars],
        }

    def screenshot(self, path: str) -> dict:
        self.page.screenshot(path=path, full_page=True)
        return {"path": path}


def tool_schemas() -> list[dict]:
    """
    JSON schemas for Anthropic tool calling.
    """
    return [
        {
            "name": "navigate",
            "description": "Navigate to a URL in the browser.",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        },
        {
            "name": "content_snapshot",
            "description": "Get a text snapshot of the page (body text) for reasoning.",
            "input_schema": {"type": "object", "properties": {"max_chars": {"type": "integer"}}, "required": []},
        },
        {
            "name": "click",
            "description": "Click the first element matching a CSS selector.",
            "input_schema": {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]},
        },
        {
            "name": "click_text",
            "description": "Click an element (button/link) by visible text.",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
        {
            "name": "type",
            "description": "Fill an input (CSS selector) with text.",
            "input_schema": {
                "type": "object",
                "properties": {"selector": {"type": "string"}, "text": {"type": "string"}},
                "required": ["selector", "text"],
            },
        },
        {
            "name": "press",
            "description": "Press a key in an element (CSS selector). Example key: Enter",
            "input_schema": {
                "type": "object",
                "properties": {"selector": {"type": "string"}, "key": {"type": "string"}},
                "required": ["selector", "key"],
            },
        },
        {
            "name": "scroll",
            "description": "Scroll down the page by dy pixels.",
            "input_schema": {"type": "object", "properties": {"dy": {"type": "integer"}}, "required": []},
        },
        {
            "name": "wait_for",
            "description": "Wait for a selector to appear.",
            "input_schema": {
                "type": "object",
                "properties": {"selector": {"type": "string"}, "timeout_ms": {"type": "integer"}},
                "required": ["selector"],
            },
        },
        {
            "name": "extract_dom",
            "description": "Extract innerText + href from nodes matching each selector.",
            "input_schema": {
                "type": "object",
                "properties": {"selectors": {"type": "array", "items": {"type": "string"}}},
                "required": ["selectors"],
            },
        },
        {
            "name": "extract_text",
            "description": "Extract innerText from the first element matching selector.",
            "input_schema": {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]},
        },
        {
            "name": "screenshot",
            "description": "Take a screenshot for debugging.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    ]

