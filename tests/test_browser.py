"""Unit tests for ProtonMailBrowser session and navigation logic (no real browser).

A small fake Page stands in for Playwright so the login/navigation decisions can
be exercised offline.
"""

import json

import pytest

from src.scraper import selectors
from src.scraper.browser import (
    ProtonMailBrowser, SessionAccountMismatchError, SessionExpiredError, SieveReadError,
    same_account,
)


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
        browser.account_email = "alice@proton.me"

        assert await browser.save_storage_state() == state_file

        assert (state_file.stat().st_mode & 0o777) == 0o600
        assert (state_file.parent.stat().st_mode & 0o777) == 0o700
        saved = json.loads(state_file.read_text())
        assert saved["cookies"] == SAMPLE_STATE["cookies"]
        assert saved["protonfusion"] == {"account_slot": 1, "account_email": "alice@proton.me"}
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
        browser.session_account_email = "alice@proton.me"
        browser.context = FakeContext(SAMPLE_STATE)

        assert await browser._reuse_saved_session() is True
        assert browser.page.visited == ["https://mail.proton.me/u/1/inbox"]

        await browser.close()
        # The email is kept even though this run never read it from the app.
        assert json.loads(state_file.read_text())["protonfusion"] == {
            "account_slot": 1, "account_email": "alice@proton.me",
        }


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


class TestLoggedUrls:
    """Page URLs in logs and errors never include the query or fragment."""

    @pytest.mark.asyncio
    async def test_stalled_session_reuse_logs_url_without_fragment(self, caplog, monkeypatch):
        monkeypatch.setattr("src.scraper.browser.SESSION_CHECK_MS", 1)
        browser = make_browser()
        browser.session_loaded = True

        async def goto(target, **kwargs):
            browser.page.url = "https://account.proton.me/fork?x=1#selector=s&sk=SECRET"

        browser.page.goto = goto
        assert await browser._reuse_saved_session() is False
        assert "SECRET" not in caplog.text
        assert "https://account.proton.me/fork" in caplog.text

    @pytest.mark.asyncio
    async def test_missing_heading_error_has_no_fragment(self):
        browser = make_browser("https://account.proton.me/u/0/mail/filters#sk=SECRET")
        with pytest.raises(RuntimeError) as excinfo:
            await browser._assert_filter_page_structure()
        assert "SECRET" not in str(excinfo.value)
        assert "https://account.proton.me/u/0/mail/filters" in str(excinfo.value)


class FakeElement:
    """A row, cell or button: an aria-label, inner text and child elements."""

    def __init__(self, aria=None, text="", children=None, on_click=None):
        self.aria = aria
        self.text = text
        self.children = children or {}
        self.on_click = on_click

    async def query_selector(self, selector):
        found = self.children.get(selector)
        return found[0] if isinstance(found, list) else found

    async def query_selector_all(self, selector):
        found = self.children.get(selector, [])
        return found if isinstance(found, list) else [found]

    async def get_attribute(self, name):
        return self.aria

    async def inner_text(self):
        return self.text

    async def click(self):
        if self.on_click:
            self.on_click()


class SievePage(FakePage):
    """A filters page holding one Sieve filter row whose Edit opens the editor."""

    def __init__(self, script="", rows=True, editor_opens=True, evaluate_error=None):
        super().__init__("https://account.proton.me/u/0/mail/filters")
        self.script = script
        self.evaluate_error = evaluate_error
        self.editor_opens = editor_opens
        edit = FakeElement(aria="Edit filter ProtonFusion Consolidated", on_click=self._open)
        row = FakeElement(children={selectors.FILTER_EDIT_BUTTON: edit})
        self.section = FakeElement(children={selectors.FILTER_TABLE_ROWS: [row] if rows else []})

    def _open(self):
        if self.editor_opens:
            self.present.add(selectors.SIEVE_EDITOR_CM)

    async def query_selector(self, selector):
        if selector == selectors.CUSTOM_FILTERS_SECTION:
            return self.section
        return None

    async def wait_for_timeout(self, ms):
        pass

    async def evaluate(self, js):
        if self.evaluate_error:
            raise self.evaluate_error
        return self.script


def sieve_browser(page) -> ProtonMailBrowser:
    browser = ProtonMailBrowser(headless=True)
    browser.page = page
    return browser


class TestReadSieveScript:
    """"" only for a genuinely absent or empty script; a failed read raises."""

    @pytest.mark.asyncio
    async def test_reads_script(self):
        browser = sieve_browser(SievePage(script="  keep;\n"))
        assert await browser.read_sieve_script("ProtonFusion Consolidated") == "keep;"

    @pytest.mark.asyncio
    async def test_empty_script_is_empty(self):
        browser = sieve_browser(SievePage(script=""))
        assert await browser.read_sieve_script("ProtonFusion Consolidated") == ""

    @pytest.mark.asyncio
    async def test_no_such_filter_is_empty(self):
        browser = sieve_browser(SievePage(rows=False))
        assert await browser.read_sieve_script("ProtonFusion Consolidated") == ""

    @pytest.mark.asyncio
    async def test_missing_filter_list_raises(self):
        browser = sieve_browser(FakePage())
        with pytest.raises(SieveReadError, match="Custom filters"):
            await browser.read_sieve_script("ProtonFusion Consolidated")

    @pytest.mark.asyncio
    async def test_editor_that_never_opens_raises(self):
        browser = sieve_browser(SievePage(script="keep;", editor_opens=False))
        with pytest.raises(SieveReadError, match="editor"):
            await browser.read_sieve_script("ProtonFusion Consolidated")

    @pytest.mark.asyncio
    async def test_editor_without_codemirror_instance_raises(self):
        browser = sieve_browser(SievePage(script=None))
        with pytest.raises(SieveReadError, match="CodeMirror"):
            await browser.read_sieve_script("ProtonFusion Consolidated")

    @pytest.mark.asyncio
    async def test_any_other_error_raises_without_url_secrets(self):
        error = TimeoutError('Timeout exceeded, navigated to "https://account.proton.me/x#sk=SECRET"')
        browser = sieve_browser(SievePage(evaluate_error=error))
        with pytest.raises(SieveReadError) as excinfo:
            await browser.read_sieve_script("ProtonFusion Consolidated")
        assert "SECRET" not in str(excinfo.value)
        assert "Timeout exceeded" in str(excinfo.value)


class TestSessionAccount:
    """A reused session must belong to the account in --credentials-file."""

    def test_load_restores_session_email(self, tmp_path):
        state_file = tmp_path / "s.json"
        state_file.write_text(json.dumps({
            **SAMPLE_STATE, "protonfusion": {"account_slot": 0, "account_email": "alice@proton.me"},
        }))
        browser = ProtonMailBrowser(headless=True, storage_state_path=state_file)
        assert "protonfusion" not in browser._load_storage_state()
        assert browser.session_account_email == "alice@proton.me"

    def test_load_without_email_is_unknown(self, tmp_path):
        state_file = tmp_path / "s.json"
        state_file.write_text(json.dumps({**SAMPLE_STATE, "protonfusion": {"account_slot": 0}}))
        browser = ProtonMailBrowser(headless=True, storage_state_path=state_file)
        browser._load_storage_state()
        assert browser.session_account_email == ""

    @pytest.mark.parametrize("username,email", [
        ("alice@proton.me", "alice@proton.me"),
        ("Alice@Proton.me", "alice@proton.me"),
        ("alice", "alice@proton.me"),
        ("alice@protonmail.com", "alice@proton.me"),
        ("alice@pm.me", "alice@protonmail.com"),
        ("alice@example.com", "alice@example.com"),
    ])
    def test_same_account(self, username, email):
        assert same_account(username, email)

    @pytest.mark.parametrize("username,email", [
        ("bob@proton.me", "alice@proton.me"),
        ("bob", "alice@proton.me"),
        ("alice@example.com", "alice@proton.me"),
        ("alice@example.com", "alice@example.org"),
    ])
    def test_different_account(self, username, email):
        assert not same_account(username, email)

    def _reused(self, monkeypatch, session_email="", live_email="", credentials=None):
        """A browser whose saved session reuse succeeds, landing in the mail app."""
        from src.utils.config import Credentials
        browser = make_browser("https://mail.proton.me/u/0/inbox", present=[selectors.COMPOSE_BUTTON])
        browser.session_loaded = True
        browser.session_account_email = session_email
        browser.credentials = Credentials(*credentials) if credentials else None

        async def capture():
            browser.account_email = live_email

        monkeypatch.setattr(browser, "_capture_account_email", capture)
        return browser

    @pytest.mark.asyncio
    async def test_mismatched_session_is_refused(self, monkeypatch):
        browser = self._reused(
            monkeypatch, session_email="alice@proton.me", live_email="alice@proton.me",
            credentials=("bob@proton.me", "pw"),
        )
        with pytest.raises(SessionAccountMismatchError, match="alice@proton.me.*bob@proton.me"):
            await browser.login()
        assert not browser._save_state_on_close  # the other account's file is left alone

    @pytest.mark.asyncio
    async def test_mismatch_caught_from_saved_email_when_app_email_unread(self, monkeypatch):
        browser = self._reused(
            monkeypatch, session_email="alice@proton.me", credentials=("bob", "pw"),
        )
        with pytest.raises(SessionAccountMismatchError):
            await browser.login()

    @pytest.mark.asyncio
    async def test_matching_session_is_reused(self, monkeypatch):
        browser = self._reused(
            monkeypatch, session_email="alice@proton.me", live_email="alice@proton.me",
            credentials=("alice", "pw"),
        )
        assert await browser.login() is True
        assert browser.page.visited == ["https://mail.proton.me/u/0/inbox"]

    @pytest.mark.asyncio
    async def test_no_credentials_means_no_check(self, monkeypatch):
        browser = self._reused(monkeypatch, session_email="alice@proton.me", live_email="alice@proton.me")
        assert await browser.login() is True

    @pytest.mark.asyncio
    async def test_unknown_account_warns_and_proceeds(self, monkeypatch, caplog):
        browser = self._reused(monkeypatch, credentials=("bob@proton.me", "pw"))
        assert await browser.login() is True
        assert "not checked against bob@proton.me" in caplog.text

    def test_cli_exits_cleanly_on_mismatch(self, monkeypatch):
        from typer.testing import CliRunner
        import src.main
        import src.scraper.protonmail_scraper as scraper_mod

        class MismatchScraper:
            def __init__(self, *args, **kwargs):
                pass

            async def initialize(self):
                pass

            async def login(self):
                raise SessionAccountMismatchError("The saved session is for alice@proton.me")

            async def close(self):
                pass

        monkeypatch.setattr(scraper_mod, "ProtonMailScraper", MismatchScraper)
        result = CliRunner().invoke(src.main.app, ["backup", "--headless"])
        assert result.exit_code == 1
        assert "alice@proton.me" in result.output
