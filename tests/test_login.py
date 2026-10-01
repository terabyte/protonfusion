"""Tests for the `login` command and ProtonMailBrowser.interactive_login (no real browser)."""

import pytest
from typer.testing import CliRunner

import src.scraper.browser
from src.main import app
from src.scraper import selectors
from src.scraper.browser import ProtonMailBrowser
from src.utils.config import Credentials

runner = CliRunner()


class RecordingBrowser:
    """Replaces ProtonMailBrowser in the CLI; records how `login` drives it."""

    instances = []
    fail_with = None

    def __init__(self, headless, credentials, storage_state_path):
        self.headless = headless
        self.credentials = credentials
        self.storage_state_path = storage_state_path
        self.account_slot = 1
        self.account_email = "me@proton.me"
        self.calls = []
        RecordingBrowser.instances.append(self)

    async def initialize(self, load_storage_state=True):
        self.calls.append(("initialize", load_storage_state))

    async def interactive_login(self, timeout_ms):
        self.calls.append(("interactive_login", timeout_ms))
        if RecordingBrowser.fail_with:
            raise RuntimeError(RecordingBrowser.fail_with)

    async def save_storage_state(self):
        self.calls.append(("save",))
        return self.storage_state_path

    async def close(self):
        self.calls.append(("close",))


@pytest.fixture
def recording_browser(monkeypatch):
    RecordingBrowser.instances = []
    RecordingBrowser.fail_with = None
    monkeypatch.setattr(src.scraper.browser, "ProtonMailBrowser", RecordingBrowser)
    return RecordingBrowser


def test_login_is_headed_fresh_and_saves(recording_browser, tmp_path):
    state = tmp_path / "s.json"
    result = runner.invoke(app, ["login", "--state", str(state), "--timeout", "90"])
    assert result.exit_code == 0, result.output
    (browser,) = recording_browser.instances
    assert browser.headless is False
    assert browser.storage_state_path == str(state)
    assert browser.calls == [
        ("initialize", False), ("interactive_login", 90000), ("save",), ("close",),
    ]
    assert "Session saved" in result.output


def test_login_prefill_credentials_passed(recording_browser, temp_credentials_file):
    result = runner.invoke(app, ["login", "--credentials-file", str(temp_credentials_file)])
    assert result.exit_code == 0, result.output
    assert recording_browser.instances[0].credentials.username


def test_login_timeout_is_clean_error(recording_browser):
    recording_browser.fail_with = "Timed out after 600s waiting for the Proton Mail inbox to load."
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 1
    assert "Timed out" in result.output
    assert ("save",) not in recording_browser.instances[0].calls
    assert ("close",) in recording_browser.instances[0].calls


class ScriptedPage:
    """Fake page for interactive_login: compose appears or never does."""

    def __init__(self, compose_appears: bool, url: str = "https://mail.proton.me/u/1/inbox", form=False):
        self.compose_appears = compose_appears
        self.form = form  # whether the username/password inputs exist
        self.clicks = []
        self.url = "about:blank"
        self.final_url = url
        self.filled = {}

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_selector(self, selector, timeout=None):
        if selector == selectors.COMPOSE_BUTTON and self.compose_appears:
            self.url = self.final_url
            return
        if self.form and selector in (selectors.USERNAME_INPUT, selectors.PASSWORD_INPUT):
            return
        raise TimeoutError(selector)

    async def click(self, selector):
        self.clicks.append(selector)

    async def fill(self, selector, value):
        self.filled[selector] = value

    async def query_selector(self, selector):
        return None


@pytest.mark.asyncio
async def test_interactive_login_records_slot(monkeypatch):
    browser = ProtonMailBrowser(headless=False)
    browser.page = ScriptedPage(compose_appears=True)
    await browser.interactive_login(timeout_ms=1000)
    assert browser.account_slot == 1


@pytest.mark.asyncio
async def test_interactive_login_timeout_says_run_login_again():
    browser = ProtonMailBrowser(headless=False)
    browser.page = ScriptedPage(compose_appears=False)
    with pytest.raises(RuntimeError, match="Run 'login' again"):
        await browser.interactive_login(timeout_ms=1000)


@pytest.mark.asyncio
async def test_prefill_failure_is_not_fatal():
    browser = ProtonMailBrowser(headless=False, credentials=Credentials("me@proton.me", "pw"))
    browser.page = ScriptedPage(compose_appears=True)  # login form never appears
    await browser.interactive_login(timeout_ms=1000)
    assert browser.account_slot == 1


@pytest.mark.asyncio
async def test_prefill_two_step_form_never_logs_password(caplog):
    caplog.set_level("DEBUG")
    browser = ProtonMailBrowser(headless=False, credentials=Credentials("me@proton.me", "hunter2-secret"))
    browser.page = ScriptedPage(compose_appears=True, form=True)
    await browser.interactive_login(timeout_ms=1000)
    assert browser.page.filled == {
        selectors.USERNAME_INPUT: "me@proton.me",
        selectors.PASSWORD_INPUT: "hunter2-secret",
    }
    assert browser.page.clicks == [selectors.LOGIN_BUTTON, selectors.LOGIN_BUTTON]
    assert "hunter2-secret" not in caplog.text


def test_expired_session_exits_cleanly(monkeypatch):
    """A browser command with a dead session prints the fix, not a traceback."""
    import src.scraper.protonmail_scraper as scraper_mod
    from src.scraper.browser import SessionExpiredError

    class ExpiredScraper:
        def __init__(self, **kwargs):
            pass

        async def initialize(self):
            pass

        async def login(self):
            raise SessionExpiredError("The saved session at /x has expired. Run 'python -m src.main login'.")

        async def close(self):
            pass

    monkeypatch.setattr(scraper_mod, "ProtonMailScraper", ExpiredScraper)
    result = runner.invoke(app, ["show", "--headless"])
    assert result.exit_code == 1
    assert "has expired" in result.output
    assert "Traceback" not in result.output
