"""Unit tests for ProtonMailBrowser session and navigation logic (no real browser).

A small fake Page stands in for Playwright so the login/navigation decisions can
be exercised offline.
"""

import json

import pytest

from src.scraper import selectors
from src.scraper.browser import ProtonMailBrowser, SessionExpiredError


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
        return object() if selector in self.present else None


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


class FakeContext:
    """Stands in for a BrowserContext's storage_state()."""

    def __init__(self, state):
        self.state = state

    async def storage_state(self):
        return dict(self.state)


SAMPLE_STATE = {"cookies": [{"name": "AUTH-x", "value": "secret"}], "origins": []}


class TestSavedSession:
    @pytest.mark.asyncio
    async def test_save_is_owner_only_and_records_slot(self, tmp_path):
        state_file = tmp_path / "newdir" / "storage_state.json"
        browser = ProtonMailBrowser(headless=True, storage_state_path=state_file)
        browser.context = FakeContext(SAMPLE_STATE)
        browser.account_slot = 1

        assert await browser.save_storage_state() == state_file

        assert (state_file.stat().st_mode & 0o777) == 0o600
        assert (state_file.parent.stat().st_mode & 0o777) == 0o700
        saved = json.loads(state_file.read_text())
        assert saved["cookies"] == SAMPLE_STATE["cookies"]
        assert saved["protonfusion"] == {"account_slot": 1}
        assert [p.name for p in state_file.parent.iterdir()] == ["storage_state.json"]

    @pytest.mark.asyncio
    async def test_save_tightens_existing_file(self, tmp_path):
        state_file = tmp_path / "storage_state.json"
        state_file.write_text("{}")
        state_file.chmod(0o644)
        browser = ProtonMailBrowser(headless=True, storage_state_path=state_file)
        browser.context = FakeContext(SAMPLE_STATE)
        await browser.save_storage_state()
        assert (state_file.stat().st_mode & 0o777) == 0o600

    def test_load_strips_meta_and_restores_slot(self, tmp_path):
        state_file = tmp_path / "s.json"
        state_file.write_text(json.dumps({**SAMPLE_STATE, "protonfusion": {"account_slot": 3}}))
        browser = ProtonMailBrowser(headless=True, storage_state_path=state_file)
        state = browser._load_storage_state()
        assert "protonfusion" not in state
        assert state["cookies"] == SAMPLE_STATE["cookies"]
        assert browser.account_slot == 3
        assert browser.session_loaded

    def test_load_missing_file(self, tmp_path):
        browser = ProtonMailBrowser(headless=True, storage_state_path=tmp_path / "absent.json")
        assert browser._load_storage_state() is None
        assert not browser.session_loaded

    @pytest.mark.parametrize("content", ["not json", "[1, 2]"])
    def test_load_corrupt_file(self, tmp_path, content):
        state_file = tmp_path / "s.json"
        state_file.write_text(content)
        browser = ProtonMailBrowser(headless=True, storage_state_path=state_file)
        assert browser._load_storage_state() is None
        assert not browser.session_loaded

    def test_env_var_still_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTONFUSION_STORAGE_STATE", str(tmp_path / "env.json"))
        assert ProtonMailBrowser().storage_state_path == tmp_path / "env.json"

    @pytest.mark.asyncio
    async def test_reuse_skipped_without_loaded_session(self):
        browser = make_browser()
        assert await browser._reuse_saved_session() is False
        assert browser.page.visited == []

    @pytest.mark.asyncio
    async def test_reused_session_goes_to_saved_slot_and_refreshes_on_close(self, tmp_path):
        state_file = tmp_path / "s.json"
        browser = make_browser(present=[selectors.COMPOSE_BUTTON])
        browser.storage_state_path = state_file
        browser.session_loaded = True
        browser.account_slot = 1
        browser.context = FakeContext(SAMPLE_STATE)

        assert await browser._reuse_saved_session() is True
        assert browser.page.visited == ["https://mail.proton.me/u/1/inbox"]

        await browser.close()
        assert json.loads(state_file.read_text())["protonfusion"] == {"account_slot": 1}


class TestExpiredSession:
    """A dead or missing session must say 'run login', not hang on a CAPTCHA."""

    def _expired(self, headless, credentials=None, url="https://account.proton.me/login"):
        browser = make_browser()
        browser.headless = headless
        browser.credentials = credentials
        browser.session_loaded = True

        async def goto(target, **kwargs):
            browser.page.visited.append(target)
            # Proton bounces a dead session to the login page.
            browser.page.url = url

        browser.page.goto = goto
        return browser

    @pytest.mark.asyncio
    async def test_headless_expired_session_raises_with_fix(self):
        browser = self._expired(headless=True)
        with pytest.raises(SessionExpiredError, match="login"):
            await browser.login()
        assert browser.page.visited == ["https://mail.proton.me/u/0/inbox"]

    @pytest.mark.asyncio
    async def test_headless_expired_session_with_credentials_still_raises(self):
        from src.utils.config import Credentials
        browser = self._expired(headless=True, credentials=Credentials("u", "p"))
        with pytest.raises(SessionExpiredError):
            await browser.login()

    @pytest.mark.asyncio
    async def test_headed_expired_session_falls_back_to_login(self, monkeypatch):
        browser = self._expired(headless=False)
        manual = []

        async def fake_manual():
            manual.append(True)
            return True

        monkeypatch.setattr(browser, "_manual_login", fake_manual)
        monkeypatch.setattr("src.scraper.browser.POST_LOGIN_SETTLE_MS", 1)
        assert await browser.login() is True
        assert manual == [True]
        assert browser.page.visited[-1] == "https://account.proton.me/login"
        assert browser._save_state_on_close  # the dead session file gets replaced

    @pytest.mark.asyncio
    async def test_headless_without_session_or_credentials_raises(self):
        browser = make_browser()
        with pytest.raises(SessionExpiredError, match="No saved session"):
            await browser.login()
        assert browser.page.visited == []

    @pytest.mark.asyncio
    async def test_headless_with_credentials_and_no_session_tries_login(self, monkeypatch):
        from src.utils.config import Credentials
        browser = make_browser()
        browser.credentials = Credentials("u", "p")
        tried = []

        async def fake_automated():
            tried.append(True)
            return True

        monkeypatch.setattr(browser, "_automated_login", fake_automated)
        assert await browser.login() is True
        assert tried == [True]

    @pytest.mark.asyncio
    async def test_login_redirect_detected_without_waiting_out_timeout(self):
        browser = make_browser("https://account.proton.me/login?product=mail")
        # A 60s budget would hang the test if the redirect were not noticed.
        assert await browser._wait_for_mail_app_or_login(60000) is False
