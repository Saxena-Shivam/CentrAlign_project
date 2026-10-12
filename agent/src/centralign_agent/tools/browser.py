from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

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

    def _resolve_locator(self, page, selector: str):
        observed_element = re.fullmatch(r"#?element-(\d+)", selector.strip())
        if observed_element:
            actual_id_selector = f"#{selector.lstrip('#')}"
            if page.locator(actual_id_selector).count():
                return page.locator(actual_id_selector)
            elements = page.locator("input, button, select, textarea, a")
            index = int(observed_element.group(1))
            if index < elements.count():
                return elements.nth(index)
            raise ValueError(f"Observed element selector {selector!r} is no longer present.")
        return page.locator(self._resolve_selector(page, selector))

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
                const describedBy = (element.getAttribute('aria-describedby') || '')
                    .split(/\\s+/).filter(Boolean);
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
                    href: element.tagName === 'A' ? element.href : '',
                    description: describedBy
                        .map(id => element.ownerDocument.getElementById(id)?.innerText.trim() || '')
                        .filter(Boolean).join(' '),
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
        activity_items = page.locator("ol.activity-list > li").evaluate_all(
            """items => items.map(item => {
                const message = item.querySelector('strong')?.innerText.trim() || '';
                const timestamp = item.querySelector('time')?.innerText.trim() || '';
                return [message, timestamp].filter(Boolean).join(' — ');
            }).filter(Boolean)"""
        )
        return {
            "status": "ok",
            "url": page.url,
            "title": page.title(),
            "text": text,
            "interactive_elements": interactive,
            "tables": tables,
            "activity_items": activity_items,
        }

    def payable_form_details(self) -> dict[str, Any]:
        page = self._ensure_page()
        self._check_current_origin(page)
        if urlsplit(page.url).path.rstrip("/") not in {"/payables", "/payables/new"}:
            raise ValueError("The current page is not the payables form.")
        observation = self._observe(page)
        labels = {
            "vendor": "vendor",
            "invoice number": "invoice_number",
            "amount": "amount",
            "amount (usd)": "amount",
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
            if self.is_payable_submit_click({"selector": args.selector}):
                raise ValueError(
                    "Submitting a payable by clicking its save button is blocked. "
                    "Use submit_form so the runtime approval check can run."
                )
            observed_href = re.fullmatch(r"""a\[href=(['"])(.*?)\1\]""", args.selector.strip())
            if observed_href:
                href = observed_href.group(2)
                target_url = urlsplit(href)
                if target_url.scheme or target_url.netloc:
                    ensure_allowed_origin(href, self.allowed_origin)
                links = page.locator("a").evaluate_all(
                    "(anchors) => anchors.map((anchor) => anchor.href)"
                )
                resolved_url = urljoin(page.url, href)
                matching_links = [
                    index for index, link_url in enumerate(links)
                    if link_url == resolved_url
                ]
                if len(matching_links) != 1:
                    raise ValueError(
                        f"Expected one visible link for {href!r}, found {len(matching_links)}."
                    )
                page.locator("a").nth(matching_links[0]).click(timeout=5000)
            else:
                self._resolve_locator(page, args.selector).click(timeout=5000)
            self._check_current_origin(page)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            return self._error(f"Click failed: {exc}", page)

    def is_payable_submit_click(self, arguments: dict[str, Any]) -> bool:
        args = ClickArgs.model_validate(arguments)
        page = self._ensure_page()
        self._check_current_origin(page)
        if urlsplit(page.url).path.rstrip("/") != "/payables/new":
            return False

        target = self._resolve_locator(page, args.selector)
        if target.count() != 1:
            return False
        return bool(
            target.evaluate(
                """element => {
                    const submit = element.closest('button, input[type="submit"]');
                    if (!submit || submit.type !== 'submit') return false;
                    const form = submit.closest('form');
                    return Boolean(
                        form &&
                        form.querySelector('input[type="number"]') &&
                        form.querySelector('input[type="date"]') &&
                        form.querySelectorAll('input[required]').length >= 3
                    );
                }"""
            )
        )

    def fill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        args = FillArgs.model_validate(arguments)
        page = None
        try:
            page = self._ensure_page()
            self._check_current_origin(page)
            field = self._resolve_locator(page, args.selector)
            field.fill(args.value, timeout=5000)
            actual_value = field.evaluate(
                "(element) => 'value' in element ? element.value : element.innerText"
            )
            if actual_value != args.value:
                raise ValueError(
                    f"Field fill was not retained: expected {args.value!r}, "
                    f"but the field contains {actual_value!r}."
                )
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
                field = self._resolve_locator(page, selector)
                field.fill(value, timeout=5000)
                actual_value = field.evaluate(
                    "(element) => 'value' in element ? element.value : element.innerText"
                )
                if actual_value != value:
                    raise ValueError(
                        f"Field {selector!r} did not retain the requested value: "
                        f"expected {value!r}, got {actual_value!r}."
                    )
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
            self._resolve_locator(page, args.selector).select_option(args.value, timeout=5000)
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
            form = self._resolve_locator(page, args.selector)
            if args.selector.strip() == "form" and form.count() > 1:
                candidates = page.locator("form").filter(
                    has=page.locator("input[required], select[required], textarea[required]")
                )
                if candidates.count() != 1:
                    raise ValueError("The page has multiple forms and no unique required-field form to submit.")
                form = candidates
            with page.expect_navigation(wait_until="domcontentloaded", timeout=5000):
                form.evaluate("(form) => form.requestSubmit()")
            self._check_current_origin(page)
            self._wait_after_action(page)
            return self._observe(page)
        except Exception as exc:
            if page is not None and "Timeout" in type(exc).__name__:
                return self._observe(page)
            return self._error(f"Submit failed: {exc}", page)
