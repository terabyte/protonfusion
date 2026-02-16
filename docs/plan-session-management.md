# Plan: Single-Session Commands & Session Persistence

## Context

Two UX issues with browser session management:

1. **Double login**: `cleanup` and `restore` commands open two separate browser sessions (scraper for reading, then sync for writing), requiring the user to log in twice. Especially painful with 2FA/manual login.

2. **No session reuse across invocations**: Every CLI command launches a fresh browser and requires full login. For 2FA accounts, this means manual intervention every time. Playwright supports saving/restoring browser state (cookies + localStorage), which would let a single manual login persist across multiple commands for several hours.

## Part 1: Single-Session Commands

### Approach: Session Transfer

Add a `transfer_session_to()` method on `ProtonMailBrowser` that hands off the live Playwright objects (browser, context, page) to a new instance of a different subclass. The donor's references are nullified so its `close()` becomes a no-op.

This is cleaner than the alternatives:
- Merging scraper + sync into one class would mix read/write concerns
- Keeping the scraper open and creating a sync client with the same context is functionally the same but messier

### Changes

**`src/scraper/browser.py`** — Add `transfer_session_to()`:
```python
async def transfer_session_to(self, target_cls, **kwargs):
    """Transfer live browser session to a new instance of target_cls.
    Returns the new instance. This instance becomes inert (close() is a no-op).
    """
    instance = target_cls(headless=self.headless, credentials=self.credentials, **kwargs)
    instance._playwright = self._playwright
    instance.browser = self.browser
    instance.context = self.context
    instance.page = self.page
    instance.account_email = self.account_email

    self._playwright = None
    self.browser = None
    self.context = None
    self.page = None
    return instance
```

Note: `ProtonMailSync` has no `__init__` override. `ProtonMailScraper.__init__` only adds `self._folder_path_map = None`, which `target_cls(...)` handles. The browser fields are then overwritten.

**`src/main.py` — `cleanup` command** (lines 815-883): Refactor from two sessions to one:
```
Before: scraper(init→login→scrape→close) → Y/N → sync(init→login→delete→close)
After:  scraper(init→login→scrape) → Y/N → transfer to sync(navigate→delete→close)
```

The browser stays open during the `typer.confirm()` prompt. After confirmation, transfer the session to a `ProtonMailSync` instance, call `navigate_to_filters()` to reset page state, then proceed with deletions. The `finally` block closes whichever instance owns the session.

**`src/main.py` — `restore` command** (lines 756-792): Same pattern. Currently scrapes, closes, re-opens for sync. Refactor to transfer session after scraping.

## Part 2: Session Persistence

### Approach: Playwright Storage State

Playwright's `context.storage_state()` serializes cookies and localStorage to JSON. `browser.new_context(storage_state="path")` restores them. If ProtonMail's session cookies are still valid, the user lands directly in the mail app without hitting the login page.

### New CLI Flag

Add `--reuse-session` (opt-in) to all browser-using commands (`backup`, `show`, `sync`, `restore`, `cleanup`, `diff` when comparing vs current). When enabled:
1. `initialize()` loads saved state from `.protonfusion/session.json` if it exists
2. Before calling `login()`, check if already authenticated (navigate to inbox, check URL)
3. If authenticated → skip login entirely
4. If not → normal login flow (automated or manual)
5. On `close()`, save current state back to the session file

Also add a `clear-session` command to delete the session file.

### Changes

**`src/utils/config.py`** — Add session path constants:
```python
SESSION_DIR = PROJECT_ROOT / ".protonfusion"
SESSION_FILE = SESSION_DIR / "session.json"
```

**`.gitignore`** — Add `.protonfusion/` entry (contains auth cookies).

**`src/scraper/browser.py`** — Five changes:

1. **`initialize(storage_state=None)`** — Accept optional path to session state JSON. If provided and exists, pass to `browser.new_context()`. Wrap in try/except to fall back to fresh context on corrupted file.

2. **`save_session(path)`** — Call `self.context.storage_state()`, write JSON, chmod `0o600`.

3. **`is_authenticated()`** — Navigate to inbox URL, wait briefly, check if URL contains `/mail/` or `/apps` (authenticated) vs login page (not authenticated).

4. **`login_with_session(session_file=None)`** — Orchestrator method:
   - If session file loaded and `is_authenticated()` → return True (skip login)
   - Otherwise → call normal `login()` → on success, `save_session()`

5. **`close(save_session_path=None)`** — Optionally save session state before closing (captures any token refreshes during the operation).

**`src/main.py`** — For each browser command:
- Add `--reuse-session` flag
- Replace `await x.initialize()` with `await x.initialize(storage_state=session_file)`
- Replace `await x.login()` with `await x.login_with_session(session_file=session_file)`
- Replace `await x.close()` with `await x.close(save_session_path=session_file)`

Commands to update: `backup`, `show`, `sync`, `restore`, `cleanup`, `diff` (when `--backup2 current`).

Add `clear-session` command:
```python
@app.command("clear-session")
def clear_session():
    """Delete saved browser session."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        console.print("[green]Session cleared.")
    else:
        console.print("[yellow]No saved session found.")
```

### Edge Cases

- **First run (no session file)**: `storage_state` path doesn't exist → fresh context, normal login, session saved after.
- **Expired session**: `is_authenticated()` returns False → normal login, session file overwritten.
- **Corrupted session file**: `new_context(storage_state=...)` throws → catch, fall back to fresh context.
- **`--headless` + expired session + manual-login-only account**: Can't re-authenticate headlessly. Print warning: "Session expired. Run once without --headless to re-authenticate."
- **`--reuse-session` without `--credentials-file` or `--manual-login`**: Works. On first use, login falls through to manual. On subsequent uses, saved session is tried first.
- **Security**: Session file contains auth cookies. Mitigated by: `.gitignore`, `chmod 0600`, sessions expire naturally after a few hours.

## Files to Modify

| File | Changes |
|------|---------|
| `src/scraper/browser.py` | `transfer_session_to()`, `initialize(storage_state)`, `save_session()`, `is_authenticated()`, `login_with_session()`, `close(save_session_path)` |
| `src/main.py` | Refactor `cleanup`/`restore` to single session; add `--reuse-session` to 6 commands; add `clear-session` command |
| `src/utils/config.py` | `SESSION_DIR`, `SESSION_FILE` |
| `.gitignore` | `.protonfusion/` |
| `docs/design-decisions.md` | Session transfer rationale, session persistence design |
| `README.md` | `--reuse-session` flag docs, `clear-session` in commands table |

## Tests

**Unit tests** (no real browser — mock Playwright objects):
- `transfer_session_to`: donor references nullified, target has session, close on donor is no-op
- `save_session`: file created with correct permissions
- `initialize` with nonexistent/corrupted session file: falls back gracefully
- `_session_path` helper: returns path when enabled, None when disabled

**Manual verification**:
1. `cleanup` with manual login — verify only one login prompt
2. `restore` with manual login — verify only one login prompt
3. `backup --reuse-session` (first time) — logs in, saves session
4. `list-snapshots` (no browser, sanity check)
5. `show --reuse-session --headless` — reuses saved session, no login
6. Wait for session to expire → `show --reuse-session` — falls back to login, re-saves
7. `clear-session` — deletes file

## Implementation Order

1. Part 1 (session transfer) — `browser.py` + `main.py` cleanup/restore refactor
2. Part 2 (session persistence) — `config.py` + `browser.py` + `main.py` all commands + `.gitignore`
3. Tests
4. Docs
