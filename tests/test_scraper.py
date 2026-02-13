"""Unit tests for scraper logic (no browser required)."""

import pytest

from src.scraper.protonmail_scraper import (
    _distribute_indices,
    ProtonMailScraper,
    BULLET_CHARS,
)


class TestDistributeIndices:
    """Tests for _distribute_indices helper."""

    def test_even_split(self):
        result = _distribute_indices(10, 5)
        assert len(result) == 5
        assert result == [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]]

    def test_uneven_split(self):
        result = _distribute_indices(10, 3)
        assert len(result) == 3
        # 10 / 3 = 3 remainder 1 => first chunk gets extra
        assert result == [[0, 1, 2, 3], [4, 5, 6], [7, 8, 9]]

    def test_more_workers_than_filters(self):
        result = _distribute_indices(3, 10)
        assert len(result) == 3
        assert result == [[0], [1], [2]]

    def test_single_worker(self):
        result = _distribute_indices(5, 1)
        assert len(result) == 1
        assert result == [[0, 1, 2, 3, 4]]

    def test_single_filter(self):
        result = _distribute_indices(1, 5)
        assert len(result) == 1
        assert result == [[0]]

    def test_zero_total(self):
        assert _distribute_indices(0, 5) == []

    def test_zero_workers(self):
        assert _distribute_indices(10, 0) == []

    def test_all_indices_covered(self):
        """Verify no indices are lost or duplicated."""
        total = 250
        for workers in [1, 3, 5, 7, 10]:
            chunks = _distribute_indices(total, workers)
            all_indices = [idx for chunk in chunks for idx in chunk]
            assert sorted(all_indices) == list(range(total))

    def test_contiguous_chunks(self):
        """Each chunk should be a contiguous range."""
        chunks = _distribute_indices(17, 4)
        for chunk in chunks:
            for i in range(1, len(chunk)):
                assert chunk[i] == chunk[i - 1] + 1


class TestResolveFolderPath:
    """Tests for _resolve_folder_path (no browser needed)."""

    def _make_scraper(self, folder_map=None):
        scraper = ProtonMailScraper.__new__(ProtonMailScraper)
        scraper._folder_path_map = folder_map
        return scraper

    def test_exact_match(self):
        scraper = self._make_scraper({"MyFolder": "MyFolder"})
        assert scraper._resolve_folder_path("MyFolder") == "MyFolder"

    def test_nested_folder(self):
        scraper = self._make_scraper({
            "Parent": "Parent",
            "• Child": "Parent/Child",
            "Child": "Parent/Child",
        })
        assert scraper._resolve_folder_path("• Child") == "Parent/Child"

    def test_stripped_match(self):
        scraper = self._make_scraper({
            "Child": "Parent/Child",
        })
        assert scraper._resolve_folder_path("• Child") == "Parent/Child"

    def test_no_map_fallback(self):
        scraper = self._make_scraper(None)
        assert scraper._resolve_folder_path("• SomeFolder") == "SomeFolder"

    def test_empty_map_fallback(self):
        scraper = self._make_scraper({})
        assert scraper._resolve_folder_path("• Nested") == "Nested"

    def test_no_match_returns_clean(self):
        scraper = self._make_scraper({"Other": "Other"})
        assert scraper._resolve_folder_path("Unknown") == "Unknown"
