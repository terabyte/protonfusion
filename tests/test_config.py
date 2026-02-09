"""Tests for configuration management."""

import pytest
from pathlib import Path

from src.utils.config import (
    load_credentials, Credentials,
    BACKUPS_DIR, OUTPUT_DIR, TOOL_VERSION,
    PROTONMAIL_LOGIN_URL, PROTONMAIL_SETTINGS_FILTERS_URL,
)


class TestLoadCredentials:
    """Test credential loading."""

    def test_load_credentials_success(self, temp_credentials_file):
        """Test loading valid credentials file."""
        creds = load_credentials(str(temp_credentials_file))

        assert isinstance(creds, Credentials)
        assert creds.username == "test@proton.me"
        assert creds.password == "testpass123"

    def test_load_credentials_file_not_found(self, tmp_path):
        """Test loading non-existent credentials file."""
        nonexistent = tmp_path / "nonexistent.txt"

        with pytest.raises(FileNotFoundError):
            load_credentials(str(nonexistent))

    def test_load_credentials_invalid_format(self, tmp_path):
        """Test loading credentials file with invalid format."""
        bad_file = tmp_path / "bad_creds.txt"
        bad_file.write_text("This is not a valid format\n")

        with pytest.raises(ValueError, match="Invalid credentials file format"):
            load_credentials(str(bad_file))

    def test_load_credentials_missing_username(self, tmp_path):
        """Test loading credentials file missing username."""
        bad_file = tmp_path / "missing_user.txt"
        bad_file.write_text("Password: testpass\n")

        with pytest.raises(ValueError, match="Invalid credentials file format"):
            load_credentials(str(bad_file))

    def test_load_credentials_missing_password(self, tmp_path):
        """Test loading credentials file missing password."""
        bad_file = tmp_path / "missing_pass.txt"
        bad_file.write_text("Username: test@proton.me\n")

        with pytest.raises(ValueError, match="Invalid credentials file format"):
            load_credentials(str(bad_file))

    def test_load_credentials_with_whitespace(self, tmp_path):
        """Test that credentials are trimmed of whitespace."""
        creds_file = tmp_path / "whitespace.txt"
        creds_file.write_text("Username:   test@proton.me   \nPassword:   testpass   \n")

        creds = load_credentials(str(creds_file))

        assert creds.username == "test@proton.me"
        assert creds.password == "testpass"

    def test_load_credentials_with_extra_lines(self, tmp_path):
        """Test loading credentials with extra lines."""
        creds_file = tmp_path / "extra.txt"
        creds_file.write_text(
            "# Comment line\n"
            "Username: test@proton.me\n"
            "Password: testpass\n"
            "Extra: ignored\n"
        )

        creds = load_credentials(str(creds_file))

        assert creds.username == "test@proton.me"
        assert creds.password == "testpass"

    def test_load_credentials_case_sensitive(self, tmp_path):
        """Test that field names are case-sensitive."""
        creds_file = tmp_path / "case.txt"
        creds_file.write_text("username: test@proton.me\npassword: testpass\n")

        # Should fail because lowercase username/password won't match
        with pytest.raises(ValueError):
            load_credentials(str(creds_file))

    def test_load_credentials_with_colon_in_password(self, tmp_path):
        """Test loading credentials where password contains colon."""
        creds_file = tmp_path / "colon.txt"
        creds_file.write_text("Username: test@proton.me\nPassword: pass:with:colons\n")

        creds = load_credentials(str(creds_file))

        assert creds.password == "pass:with:colons"

    def test_load_credentials_empty_values(self, tmp_path):
        """Test that empty username/password raises error."""
        creds_file = tmp_path / "empty.txt"
        creds_file.write_text("Username: \nPassword: \n")

        with pytest.raises(ValueError, match="Invalid credentials file format"):
            load_credentials(str(creds_file))


class TestCredentialsDataclass:
    """Test Credentials dataclass."""

    def test_create_credentials(self):
        """Test creating Credentials object."""
        creds = Credentials(username="test@proton.me", password="testpass")

        assert creds.username == "test@proton.me"
        assert creds.password == "testpass"

    def test_credentials_immutable_like(self):
        """Test that credentials can be accessed."""
        creds = Credentials(username="user", password="pass")

        # Should be able to read
        assert creds.username == "user"
        assert creds.password == "pass"


class TestConstants:
    """Test configuration constants."""

    def test_backups_dir_exists(self):
        """Test that BACKUPS_DIR is a Path."""
        assert isinstance(BACKUPS_DIR, Path)

    def test_output_dir_exists(self):
        """Test that OUTPUT_DIR is a Path."""
        assert isinstance(OUTPUT_DIR, Path)

    def test_tool_version(self):
        """Test that TOOL_VERSION is set."""
        assert isinstance(TOOL_VERSION, str)
        assert len(TOOL_VERSION) > 0

    def test_protonmail_urls(self):
        """Test that ProtonMail URLs are defined."""
        assert PROTONMAIL_LOGIN_URL.startswith("https://")
        assert PROTONMAIL_SETTINGS_FILTERS_URL.startswith("https://")
        assert "proton" in PROTONMAIL_LOGIN_URL.lower()
        assert "proton" in PROTONMAIL_SETTINGS_FILTERS_URL.lower()

    def test_timeout_values(self):
        """Test that timeout values are imported."""
        from src.utils.config import (
            LOGIN_TIMEOUT_MS, PAGE_LOAD_TIMEOUT_MS, ELEMENT_TIMEOUT_MS
        )

        assert LOGIN_TIMEOUT_MS > 0
        assert PAGE_LOAD_TIMEOUT_MS > 0
        assert ELEMENT_TIMEOUT_MS > 0


class TestDirectoryCreation:
    """Test that directories are created on import."""

    def test_backups_dir_created(self):
        """Test that backups directory exists."""
        # BACKUPS_DIR should exist after import
        assert BACKUPS_DIR.exists()
        assert BACKUPS_DIR.is_dir()

    def test_output_dir_created(self):
        """Test that output directory exists."""
        # OUTPUT_DIR should exist after import
        assert OUTPUT_DIR.exists()
        assert OUTPUT_DIR.is_dir()


class TestPathResolution:
    """Test path resolution."""

    def test_project_root_is_parent(self):
        """Test that project root is correctly identified."""
        from src.utils.config import PROJECT_ROOT

        assert isinstance(PROJECT_ROOT, Path)
        # Should contain src directory
        assert (PROJECT_ROOT / "src").exists()

    def test_default_credentials_path(self):
        """Test default credentials file path."""
        from src.utils.config import DEFAULT_CREDENTIALS_FILE

        assert isinstance(DEFAULT_CREDENTIALS_FILE, Path)
        assert DEFAULT_CREDENTIALS_FILE.name == ".credentials"

    def test_paths_are_absolute(self):
        """Test that configured paths are absolute."""
        assert BACKUPS_DIR.is_absolute()
        assert OUTPUT_DIR.is_absolute()

    def test_backups_under_project_root(self):
        """Test that backups dir is under project root."""
        from src.utils.config import PROJECT_ROOT

        # BACKUPS_DIR should be a subdirectory of PROJECT_ROOT
        assert BACKUPS_DIR.parent == PROJECT_ROOT or PROJECT_ROOT in BACKUPS_DIR.parents

    def test_output_under_project_root(self):
        """Test that output dir is under project root."""
        from src.utils.config import PROJECT_ROOT

        # OUTPUT_DIR should be a subdirectory of PROJECT_ROOT
        assert OUTPUT_DIR.parent == PROJECT_ROOT or PROJECT_ROOT in OUTPUT_DIR.parents
