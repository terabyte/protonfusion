"""Unit tests for ProtonMailSync.upload_sieve's success checks (no browser).

upload_sieve must report success only when the script really landed: the
editor took it, the filter reads back with the same script, and the filter
is enabled. Anything else returns False so sync re-enables the filters it
disabled. The page is faked with just the calls upload_sieve makes; the
list-level helpers (open by name, read back, enabled state) are stubbed.
"""

import asyncio

import pytest

from src.scraper import selectors
from src.scraper.browser import SieveReadError
from src.scraper.protonmail_sync import ProtonMailSync, normalize_script

SCRIPT = 'require ["fileinto"];\nif header :contains "Subject" "x" { fileinto "X"; }\n'


class FakeButton:
    """The Sieve editor's Save button."""

    def __init__(self, page):
        self.page = page

    async def is_disabled(self):
        return False

    async def click(self):
        self.page.saved = True


class FakeUploadPage:
    """The Sieve editor: CodeMirror present, setValue reporting `editor_status`."""

    def __init__(self, editor_status="ok"):
        self.editor_status = editor_status
        self.evaluated = []
        self.saved = False

    async def wait_for_selector(self, selector, timeout=None):
        return object()

    async def evaluate(self, js, arg=None):
        self.evaluated.append(arg)
        return self.editor_status

    async def query_selector(self, selector):
        if selector == selectors.SIEVE_SAVE_BUTTON:
            return FakeButton(self)
        return None

    async def wait_for_timeout(self, ms):
        pass


def _upload(editor_status="ok", read_back=SCRIPT, enabled=True):
    """Run upload_sieve against the fakes; return (result, page).

    `read_back` is what reading the filter again returns, or an exception
    to raise; `enabled` is the filter's toggle state after saving.
    """
    sync = ProtonMailSync()
    sync.page = FakeUploadPage(editor_status)

    async def open_by_name(name):
        return True

    async def read_sieve_script(filter_name=""):
        if isinstance(read_back, Exception):
            raise read_back
        return read_back

    async def ensure_enabled(name):
        pass

    async def filter_is_enabled(name):
        return enabled

    sync._open_sieve_filter_by_name = open_by_name
    sync.read_sieve_script = read_sieve_script
    sync._ensure_filter_enabled = ensure_enabled
    sync._filter_is_enabled = filter_is_enabled
    result = asyncio.run(sync.upload_sieve(SCRIPT, filter_name="ProtonFusion Consolidated"))
    return result, sync.page


def test_verified_upload_succeeds():
    result, page = _upload()
    assert result is True
    assert page.saved


def test_editor_without_codemirror_fails_without_saving():
    result, page = _upload(editor_status="no-editor")
    assert result is False
    assert not page.saved


def test_read_back_mismatch_fails():
    result, _ = _upload(read_back='require ["fileinto"];\nkeep;')
    assert result is False


def test_read_back_of_nothing_fails():
    result, _ = _upload(read_back="")
    assert result is False


def test_failed_read_back_fails():
    result, _ = _upload(read_back=SieveReadError("the Sieve editor did not open"))
    assert result is False


def test_trailing_whitespace_differences_are_ignored():
    padded = "\n".join(line + "  " for line in SCRIPT.splitlines()) + "\n\n"
    result, _ = _upload(read_back=padded.strip())
    assert result is True


def test_filter_left_disabled_fails():
    result, _ = _upload(enabled=False)
    assert result is False


@pytest.mark.parametrize("a,b,same", [
    ("keep;\n", "keep;", True),
    ("a;  \nb;\t\n", "a;\nb;", True),
    ("a;\n  b;", "a;\nb;", False),
    ("keep;", "discard;", False),
])
def test_normalize_script_only_ignores_trailing_whitespace(a, b, same):
    assert (normalize_script(a) == normalize_script(b)) is same


def test_upload_error_is_logged_without_url_secrets(caplog):
    """A Playwright error naming a fragment-bearing URL must not reach the log raw."""
    sync = ProtonMailSync()
    sync.page = FakeUploadPage()

    async def open_by_name(name):
        raise TimeoutError('Timeout exceeded, navigated to "https://account.proton.me/x#sk=SECRET"')

    sync._open_sieve_filter_by_name = open_by_name
    with pytest.raises(TimeoutError):
        asyncio.run(sync.upload_sieve(SCRIPT))
    assert "Timeout exceeded" in caplog.text
    assert "SECRET" not in caplog.text
