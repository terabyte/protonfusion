"""Unit tests for ProtonMailBrowser session and navigation logic (no real browser).

A small fake Page stands in for Playwright so the login/navigation decisions can
be exercised offline.
"""

import pytest

from src.scraper import selectors
from src.scraper.browser import ProtonMailBrowser


class FakePage:
    """Minimal stand-in for a Playwright Page: a URL plus scripted behaviour."""

    def __init__(self, url: str = "about:blank", present=()):
        self.url = url
        self.visited = []
        self.present = set(present)  # selectors that "exist" on the page

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = url

    async def wait_for_selector(self, selector, timeout=None):
        if selector not in self.present:
            raise TimeoutError(f"{selector} not found")

    async def query_selector(self, selector):
        return None


def make_browser(url: str = "about:blank", present=()) -> ProtonMailBrowser:
    """A browser whose page is a FakePage (initialize() is never called)."""
    browser = ProtonMailBrowser(headless=True)
    browser.page = FakePage(url, present)
    return browser


class TestAccountSlot:
    def test_default_slot_urls(self):
        browser = make_browser()
        assert browser.mail_url("inbox") == "https://mail.proton.me/u/0/inbox"
        assert browser.account_url("mail/filters") == "https://account.proton.me/u/0/mail/filters"

    def test_record_slot_from_mail_url(self):
        browser = make_browser("https://mail.proton.me/u/1/inbox")
        browser._record_account_slot()
        assert browser.account_slot == 1
        assert browser.account_url("mail/filters") == "https://account.proton.me/u/1/mail/filters"

    def test_record_slot_ignores_url_without_slot(self):
        browser = make_browser("https://account.proton.me/apps")
        browser.account_slot = 2
        browser._record_account_slot()
        assert browser.account_slot == 2


class TestNavigateToFilters:
    """Direct URL first; the settings-menu click path only as a fallback."""

    @pytest.mark.asyncio
    async def test_direct_navigation_uses_slot(self, monkeypatch):
        browser = make_browser()
        browser.account_slot = 1
        calls = []

        async def wait():
            calls.append("wait")

        async def check():
            calls.append("assert")

        async def menu():
            calls.append("menu")

        monkeypatch.setattr(browser, "_wait_for_filters_page", wait)
        monkeypatch.setattr(browser, "_assert_filter_page_structure", check)
        monkeypatch.setattr(browser, "_navigate_to_filters_via_menu", menu)

        await browser.navigate_to_filters()

        assert browser.page.visited == ["https://account.proton.me/u/1/mail/filters"]
        assert calls == ["wait", "assert"]

    @pytest.mark.asyncio
    async def test_falls_back_to_menu_when_direct_fails(self, monkeypatch):
        browser = make_browser()
        calls = []

        async def direct():
            calls.append("direct")
            raise RuntimeError("page did not render")

        async def menu():
            calls.append("menu")

        async def wait():
            calls.append("wait")

        async def check():
            calls.append("assert")

        monkeypatch.setattr(browser, "_open_filters_directly", direct)
        monkeypatch.setattr(browser, "_navigate_to_filters_via_menu", menu)
        monkeypatch.setattr(browser, "_wait_for_filters_page", wait)
        monkeypatch.setattr(browser, "_assert_filter_page_structure", check)

        await browser.navigate_to_filters()

        assert calls == ["direct", "menu", "wait", "assert"]

    @pytest.mark.asyncio
    async def test_fallback_structure_failure_propagates(self, monkeypatch):
        browser = make_browser()

        async def direct():
            raise RuntimeError("direct failed")

        async def noop():
            pass

        async def check():
            raise RuntimeError("Missing 'Custom filters' heading")

        monkeypatch.setattr(browser, "_open_filters_directly", direct)
        monkeypatch.setattr(browser, "_navigate_to_filters_via_menu", noop)
        monkeypatch.setattr(browser, "_wait_for_filters_page", noop)
        monkeypatch.setattr(browser, "_assert_filter_page_structure", check)

        with pytest.raises(RuntimeError, match="Custom filters"):
            await browser.navigate_to_filters()


class TestAfterLogin:
    @pytest.mark.asyncio
    async def test_records_slot_and_tolerates_slow_app(self, monkeypatch):
        monkeypatch.setattr("src.scraper.browser.POST_LOGIN_SETTLE_MS", 1)
        browser = make_browser("https://mail.proton.me/u/1/inbox")  # compose never appears
        await browser._after_login()
        assert browser.account_slot == 1

    @pytest.mark.asyncio
    async def test_login_with_reused_session_skips_login_page(self, monkeypatch):
        browser = make_browser("https://mail.proton.me/u/2/inbox", present=[selectors.COMPOSE_BUTTON])

        async def reused():
            return True

        monkeypatch.setattr(browser, "_reuse_saved_session", reused)
        assert await browser.login() is True
        assert browser.page.visited == []
        assert browser.account_slot == 2
