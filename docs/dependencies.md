# Dependencies

ProtonFusion's dependencies were chosen to minimize the dependency tree while providing the specific capabilities needed for browser automation, data validation, and CLI interaction.

## Runtime Dependencies

### Playwright (`>=1.48.0`)

**What it does:** Browser automation for scraping ProtonMail's filter settings and syncing Sieve scripts back.

**Why this library:** ProtonMail has no public API for filter management. The only programmatic access is through the web UI. Playwright was chosen over Selenium and Puppeteer-Python because:

- Native async support integrates with Python's `asyncio` for parallel tab scraping.
- `BrowserContext` enables session sharing across tabs (one login, multiple scraper workers).
- Auto-wait and selector engine reduce flakiness compared to raw Selenium.
- Active maintenance and fast adoption of new browser features.

**Why not Selenium?** Selenium's async story is weaker, and its selector engine requires more manual wait logic. Playwright's auto-wait eliminates an entire class of timing bugs.

**Why not requests/httpx?** ProtonMail is a single-page application. Filters are rendered client-side after JavaScript execution. HTTP-only tools can't access the filter UI.

### Pydantic (`>=2.9.2`)

**What it does:** Data validation and serialization for filter models and backup files.

**Why this library:** Data scraped from a web UI is inherently unstructured. Pydantic validates and normalizes it at the boundary, so all downstream code (consolidation, Sieve generation, backup management) can trust the types. Pydantic v2's `model_dump()` and `model_validate()` provide clean JSON round-tripping for backup files.

**Why not dataclasses?** Dataclasses don't validate. A filter with `type="sennder"` (typo) would silently propagate. Pydantic catches this immediately. Dataclasses also lack built-in JSON serialization for nested models.

**Why not attrs?** Attrs can validate, but Pydantic's JSON schema support and ecosystem integration (especially with FastAPI patterns that developers are familiar with) made it the more pragmatic choice.

### Typer (`>=0.15.0`)

**What it does:** CLI framework. Each command is a decorated Python function.

**Why this library:** Typer generates CLI interfaces from function type hints with minimal boilerplate. It integrates natively with Rich for colored output and has a clean subcommand model.

**Why not Click?** Typer is built on Click but reduces boilerplate significantly. A Typer command is a function with type-annotated parameters; Click requires explicit `@click.option()` decorators for each parameter.

**Why not argparse?** argparse requires substantially more code for subcommands, help text, and type coercion. The result is harder to read and maintain.

**Known quirk:** Typer `>=0.15.0` is required for Click 8.x compatibility. Use `str` with `""` default instead of `Optional[str]` to avoid "secondary flag" errors.

### Rich (`>=13.9.4`)

**What it does:** Terminal formatting: tables, panels, colored text, progress spinners.

**Why this library:** ProtonFusion's CLI output includes filter tables, diff summaries, and consolidation reports. Rich makes these readable without manual ANSI escape code management.

**Why not plain print?** Filter listings with conditions, actions, and status flags are hard to read as unformatted text. Rich tables are substantially more usable.

### python-dotenv (`>=1.0.1`)

**What it does:** Loads environment variables from `.env` files.

**Why this library:** Used for development convenience (loading `PROTONFUSION_DATA_DIR` and other config from `.env`). Minimal dependency with no transitive dependencies of its own.

## Test Dependencies

### pytest (`>=8.3.4`)

Standard Python test framework. Chosen over unittest for its fixture system, parametrize support, and cleaner assertion syntax.

### pytest-playwright (`>=0.6.2`)

Provides Playwright fixtures for pytest (browser, context, page). Used in E2E tests that interact with live ProtonMail.

### pytest-asyncio (`>=0.24.0`)

Enables `async def` test functions. Required because the scraper and sync layers are async (Playwright's API is async).

## Dependency Philosophy

- **Minimal tree.** Every dependency must justify its inclusion. There are 5 runtime dependencies and 3 test dependencies.
- **No pinning to exact versions.** Version floors (`>=`) ensure compatibility without preventing security patches. For reproducible builds, use `pip freeze` to generate a lockfile.
- **No vendoring.** All dependencies are installed from PyPI. Playwright also requires a one-time `playwright install chromium` for the browser binary.
