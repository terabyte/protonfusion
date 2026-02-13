# Contributor Guide

This document explains how to contribute to ProtonFusion. All contributors must agree to and abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## How to Contribute

As an open source project, we encourage others to contribute. All contributions are valued, whether small documentation fixes or large code refactors or entirely new features. That said, code must be maintained and existing users should not be broken without good reason. Because of this, not all contributions will be ready to be accepted exactly as they are.

If you are considering contributing a change, particularly a large one, or one that changes how things work, compatibility of configuration files, APIs, etc. please make sure to file an issue and discuss your plans first. A design document is not a bad idea for larger or more technical changes. Some contributions may be rejected, or delayed until requested changes are made. As a contributor, you always have the option to reject those suggestions and leave your changes to be adopted by someone else (or not). Because our chosen license, the [UNLICENSE](UNLICENSE), does not forbid it, you also always have the choice to fork the project if you don't like it.

## Development Setup

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
3. Run the unit tests to verify your setup:
   ```bash
   python -m pytest tests/ -v
   ```

## Contribution Checklist

To contribute to the project, you should:

* Check to see if an issue already exists describing the problem. If it does, see if there is already an implementation plan discussed (or possibly even a fix already in progress). Make sure your commit message or MR mentions the issue so they will be linked.
* Ensure your changes comply with the [Code of Conduct](CODE_OF_CONDUCT.md)
* Ensure if your changes add code, they also add tests. Test coverage should not decrease significantly due to a change
* Ensure all tests (new and existing) pass reliably. Contributions whose tests do not pass may be ignored
* Ensure the code you write is unencumbered by licenses or IP concerns incompatible with the [UNLICENSE](UNLICENSE)
* Push your change to a branch prefixed with `dev/YOURNAME-ISSUENUM-DESC`, for example, `dev/cmyers-20-contributor-guide`

## Project Structure

See [docs/architecture.md](docs/architecture.md) for a detailed overview. The key directories are:

* `src/models/` - Pydantic v2 data models
* `src/scraper/` - Playwright browser automation
* `src/parser/` - Scraped data to model conversion
* `src/backup/` - Snapshot management, diffing, restore
* `src/consolidator/` - Filter optimization engine with pluggable strategies
* `src/generator/` - Sieve script generation
* `src/utils/` - Configuration and constants
* `tests/` - Unit tests (258 tests across 7 files)

## Testing

* **Unit tests** run offline and don't need a ProtonMail account: `python -m pytest tests/ -v`
* **E2E tests** require a test account with credentials in `.credentials`: `bash test_workflow.sh`
* See [docs/testing.md](docs/testing.md) for details on test structure and writing new tests

## Updating ProtonMail Selectors

ProtonMail occasionally changes their web UI. All CSS selectors are centralized in `src/scraper/selectors.py`. See [docs/protonmail-ui.md](docs/protonmail-ui.md) for the current page structure.
