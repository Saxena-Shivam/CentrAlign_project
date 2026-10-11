from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from centralign_agent.safety import ensure_allowed_origin


class NavigateArgs(BaseModel):
    url: str = Field(..., min_length=1)


class ClickArgs(BaseModel):
    selector: str = Field(..., min_length=1)


class FillArgs(BaseModel):
    selector: str = Field(..., min_length=1)
    value: str = Field(default="")


class FillFieldsArgs(BaseModel):
    fields: dict[str, str] = Field(..., min_length=1)


class SelectArgs(BaseModel):
    selector: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)


class ReadPageArgs(BaseModel):
    selector: str | None = None


class SubmitArgs(BaseModel):
    selector: str = Field(..., min_length=1)


class ScreenshotArgs(BaseModel):
    filename: str | None = None


class BrowserToolSet:
    """Browser tools backed by one page that persists for the duration of a run."""

    _SIMPLE_SELECTOR = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

    def __init__(
        self,
        allowed_origin: str,
        screenshots_dir: Path,
        headless: bool = True,
        action_delay_ms: int = 0,
    ):
        self.allowed_origin = allowed_origin.rstrip("/")
        self.screenshots_dir = screenshots_dir
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        if action_delay_ms < 0:
            raise ValueError("Browser action delay cannot be negative.")
        self.action_delay_ms = action_delay_ms if not headless else 0
        self._playwright = None
        self._browser = None
        self._page = None
        self._startup_error: str | None = None

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = None
        self._playwright = None
        self._page = None

    def _ensure_page(self):
        if self._startup_error:
            raise RuntimeError(self._startup_error)
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self._page = self._browser.new_page()
            self._page.route("**/*", self._restrict_request)
            return self._page
        except Exception as exc:
            self._startup_error = f"Could not start Playwright Chromium: {exc}"
            raise RuntimeError(self._startup_error) from exc

    def _restrict_request(self, route) -> None:
        try:
            ensure_allowed_origin(route.request.url, self.allowed_origin)
        except ValueError:
            route.abort()
        else:
            route.continue_()

    def current_url(self) -> str | None:
        if self._page is None:
            return None
        self._check_current_origin(self._page)
        return self._page.url

    def _check_current_origin(self, page) -> None:
        if page.url and page.url != "about:blank":
            ensure_allowed_origin(page.url, self.allowed_origin)

    def _wait_after_action(self, page) -> None:
        if self.action_delay_ms:
            page.wait_for_timeout(self.action_delay_ms)

    def _resolve_selector(self, page, selector: str) -> str:
        if not self._SIMPLE_SELECTOR.fullmatch(selector):
            return selector

        name_selector = f'[name="{selector}"]'
        name_count = page.locator(name_selector).count()
        if name_count == 1:
            return name_selector
        if name_count > 1:
            raise ValueError(f"Selector {selector!r} matches multiple named elements.")

        id_selector = f"#{selector}"
        id_count = page.locator(id_selector).count()
        if id_count == 1:
            return id_selector
        if id_count > 1:
            raise ValueError(f"Selector {selector!r} matches multiple elements by id.")

        if page.locator(selector).count():
            return selector
        raise ValueError(
            f"No element matches {selector!r}; use a visible element's CSS id selector "
            "or [name=\"...\"] selector."
        )

    def _observe(self, page, selector: str | None = None) -> dict[str, Any]:
        self._check_current_origin(page)
        locator = page.locator(selector or "body")
        text = locator.inner_text(timeout=3000)[:3000]
        interactive = page.locator("input, button, select, textarea, a").evaluate_all(
            """elements => elements.slice(0, 50).map((element, index) => {
                const labels = element.labels
                    ? Array.from(element.labels).map(label => label.innerText.trim()).filter(Boolean)
                    : [];
                const role = element.getAttribute('role') || (
                    element.tagName === 'A' ? 'link' :
                    element.tagName === 'BUTTON' ? 'button' :
                    element.tagName === 'SELECT' ? 'combobox' :
                    element.tagName === 'TEXTAREA' ? 'textbox' :
                    element.type === 'submit' ? 'button' : element.type || 'textbox'
                );
                const isPassword = element.type === 'password';
                return {
                    id: element.id || `element-${index}`,
                    tag: element.tagName.toLowerCase(),
                    role,
                    label: labels[0] || element.getAttribute('aria-label') ||
                        element.getAttribute('placeholder') || element.innerText.trim() || '',
                    name: element.getAttribute('name') || '',
                    value: isPassword && element.value ? '[redacted]' : (element.value || ''),
                    filled: Boolean(element.value),
                    required: Boolean(element.required),
                    options: element.tagName === 'SELECT'
                        ? Array.from(element.options).map(option => ({
                            label: option.innerText.trim(), value: option.value
                        }))
                        : []
                };
            })"""
        )
        tables = page.locator("table").evaluate_all(
            """tables => tables.map(table => {
                const headers = Array.from(table.querySelectorAll('thead th'))
                    .map(cell => cell.innerText.trim());
                const rows = Array.from(table.querySelectorAll('tbody tr'))
                    .map(row => Array.from(row.querySelectorAll('th, td'))
                        .map(cell => cell.innerText.trim()))
                    .filter(row => row.length > 0);
                return { headers, rows };
            })"""
        )
        return {
            "status": "ok",
            "url": page.url,
            "title": page.title(),
            "text": text,
            "interactive_elements": interactive,
            "tables": tables,
        }

    def payable_form_details(self) -> dict[str, Any]:
        page = self._ensure_page()
        self._check_current_origin(page)
        if urlsplit(page.url).path.rstrip("/") != "/payables":
            raise ValueError("The current page is not the payables form.")
        observation = self._observe(page)
        labels = {
            "vendor": "vendor",
            "invoice number": "invoice_number",
            "amount": "amount",
            "due date": "due_date",
            "approval code": "approval_code",
        }
        fields = {}
        for element in observation["interactive_elements"]:
            name = element.get("name", "")
            key = name if name in labels.values() else labels.get(str(element.get("label", "")).strip().lower())
            if key:
                fields[key] = element.get("value", "")
        return {"url": page.url, "fields": fields}

    def _error(self, message: str, page=None) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "error", "message": message}
        if page is not None:
            try:
                result["current_page"] = self._observe(page)
            except Exception as exc:
                result["observation_error"] = str(exc)
        return result

    def navigate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = NavigateArgs.model_validate(arguments)
        try:
            ensure_allowed_origin(args.url, self.allowed_origin)
            page = self._ensure_page()
            page.goto(args.url, wait_until="domcontentloaded")
            self._check_current_origin(page)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            return self._error(f"Browser navigation failed: {exc}", self._page)

    def click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = ClickArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            self._check_current_origin(page)
            selector = self._resolve_selector(page, args.selector)
            page.locator(selector).click(timeout=5000)
            self._check_current_origin(page)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            return self._error(f"Click failed: {exc}", page)

    def fill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = FillArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            self._check_current_origin(page)
            selector = self._resolve_selector(page, args.selector)
            page.locator(selector).fill(args.value, timeout=5000)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            return self._error(f"Fill failed: {exc}", page)

    def fill_fields(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = FillFieldsArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            self._check_current_origin(page)
            for selector, value in args.fields.items():
                resolved_selector = self._resolve_selector(page, selector)
                page.locator(resolved_selector).fill(value, timeout=5000)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            return self._error(f"Filling form fields failed: {exc}", page)

    def select_option(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = SelectArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            self._check_current_origin(page)
            selector = self._resolve_selector(page, args.selector)
            page.locator(selector).select_option(args.value, timeout=5000)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            return self._error(f"Selection failed: {exc}", page)

    def read_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = ReadPageArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            return self._observe(page, args.selector)
        except Exception as exc:
            return self._error(f"Read page failed: {exc}", page)

    def take_screenshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = ScreenshotArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            self._check_current_origin(page)
            filename = args.filename or f"step-{len(list(self.screenshots_dir.glob('*.png'))) + 1:03d}.png"
            if Path(filename).name != filename or not filename.lower().endswith(".png"):
                raise ValueError("Screenshot filename must be a simple .png filename.")
            path = self.screenshots_dir / filename
            page.screenshot(path=str(path), full_page=True)
            return {**self._observe(page), "path": str(path)}
        except Exception as exc:
            return self._error(f"Screenshot failed: {exc}", page)

    def submit_form(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = SubmitArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            self._check_current_origin(page)
            with page.expect_navigation(wait_until="domcontentloaded", timeout=5000):
                selector = self._resolve_selector(page, args.selector)
                page.locator(selector).evaluate("(form) => form.requestSubmit()")
            self._check_current_origin(page)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            if page is not None and "Timeout" in type(exc).__name__:
                return self._observe(page)
            return self._error(f"Submit failed: {exc}", page)
