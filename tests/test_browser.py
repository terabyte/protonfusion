"""Unit tests for ProtonMailBrowser session and navigation logic (no real browser).

A small fake Page stands in for Playwright so the login/navigation decisions can
be exercised offline.
"""

import pytest

from src.scraper.browser import ProtonMailBrowser


class FakePage:
    """Minimal stand-in for a Playwright Page: a URL plus scripted behaviour."""

    def __init__(self, url: str = "about:blank"):
        self.url = url
        self.visited = []

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = url


def make_browser(url: str = "about:blank") -> ProtonMailBrowser:
    """A browser whose page is a FakePage (initialize() is never called)."""
    browser = ProtonMailBrowser(headless=True)
    browser.page = FakePage(url)
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
