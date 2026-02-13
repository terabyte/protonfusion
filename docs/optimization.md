# Scraping Performance Optimization

## Problem

Scraping filters from ProtonMail is slow because each filter requires opening an edit modal and clicking through a 3-step wizard (Name, Conditions, Actions). The minimum wait per filter is ~5 seconds:

- 3 x 1500ms modal transition waits (`MODAL_TRANSITION_MS`)
- 1 x 500ms modal close wait (`DROPDOWN_MS`)

With 250 filters scraped sequentially, that's 250 x 5s = **~21 minutes**.

## Solution: Parallel Browser Tabs

We open N browser tabs (Playwright pages) within the same `BrowserContext`, sharing the login session. Each worker tab navigates directly to the filters page, scrapes its assigned chunk of filters, and results are merged by index to preserve priority ordering.

### Results

| Filters | Workers | Theoretical | Actual | Speedup |
|---------|---------|-------------|--------|---------|
| 250     | 1       | ~21 min     | ~21 min | 1x     |
| 250     | 5       | ~4.2 min    | ~9 min  | ~2.3x  |

The actual time exceeds the theoretical minimum by ~4.8 minutes due to overhead sources described below.

### Why actual is slower than theoretical

- **Worker startup**: each tab navigates to the filters page + a 3s `FILTERS_PAGE_LOAD_MS` safety wait (~15s total for 5 workers).
- **Asyncio single-thread contention**: all workers share one event loop. They interleave at `await` points but DOM queries and Chromium IPC calls serialize. With 5 workers, the fixed `wait_for_timeout` calls overlap well, but non-wait work doesn't truly parallelize.
- **Folder path map building**: the first worker to encounter a folder action opens the dropdown, reads all items, and closes it (~1s), blocking other workers briefly. Subsequent workers reuse the cached map.
- **ProtonMail server latency**: each modal open involves a server round-trip, not just a CSS transition. The 1500ms wait is a safety margin; real latency varies.

## Future Optimization Opportunities

| Optimization | Effort | Expected Gain | Trade-offs |
|---|---|---|---|
| Replace fixed `wait_for_timeout` with `wait_for_selector` on step content | Medium | Eliminates wasted wait on fast modals; best remaining gain | Needs careful selector work to detect "modal fully loaded"; risk of flaky scraping if ProtonMail load times vary |
| Reduce `MODAL_TRANSITION_MS` from 1500ms to 800ms | Trivial (1 line) | ~30% faster per-filter | Risky if ProtonMail is slow; modals could be scraped before content loads |
| Increase workers beyond 5 (up to 10) | Already supported via `--workers` flag | Diminishing returns due to asyncio contention | ProtonMail may rate-limit; more browser tabs consume more memory |
| Use ProtonMail API instead of UI scraping | Large | Would eliminate scraping entirely | ProtonMail does not offer a public filter management API |

### Recommendation

The parallel tabs optimization captures the main win. The remaining time is dominated by fixed per-filter wait overhead that can only be reduced (not parallelized away) by switching to smarter waits (`wait_for_selector`). That change is worthwhile only if 9 minutes remains painful — it's a reliability-sensitive optimization that requires testing against real ProtonMail latency variance.

## What is NOT parallelized (and why)

Only scraping (read-only operations) is parallelized. Sync operations in `protonmail_sync.py` remain sequential because they mutate shared server-side state:

- **Disabling filters**: tabs would race on the same toggle buttons; DOM row references go stale as other tabs change state.
- **Deleting filters**: the filter list DOM shifts as items are removed; index-based references break across tabs.
- **Sieve upload**: single operation, nothing to parallelize.
- **Filter creation**: sequential wizard, no benefit from parallelism.
