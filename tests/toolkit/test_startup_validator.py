"""The toolkit's config validator — startup validation and provenance logging."""

from __future__ import annotations

import logging

import pytest
from custos_toolkit.config import (
    ValidationResult,
    abort_on_failure,
    log_provenance,
    validate_startup,
)


class TestValidateStartup:
    """Tests for validate_startup() — RTF-02."""

    def test_valid_strategy_passes(self, tmp_path):
        """validate_startup returns ok=True when strategy dir and config.yaml exist with valid YAML."""
        strategy_dir = tmp_path / "mystrat"
        strategy_dir.mkdir()
        (strategy_dir / "config.yaml").write_text("key: value\n")

        result = validate_startup("mystrat", "nautilus", strategy_root=tmp_path)

        assert result.ok is True
        assert result.failures == []

    def test_missing_dir_reported(self, tmp_path):
        """validate_startup returns ok=False with [MISSING] directory failure when dir does not exist."""
        result = validate_startup("nonexistent_strategy", "nautilus", strategy_root=tmp_path)

        assert result.ok is False
        assert any("[MISSING]" in f and "directory" in f.lower() for f in result.failures)

    def test_missing_config_reported(self, tmp_path):
        """validate_startup returns ok=False with [MISSING] config.yaml failure when dir exists but no config."""
        strategy_dir = tmp_path / "mystrat"
        strategy_dir.mkdir()
        # No config.yaml created

        result = validate_startup("mystrat", "nautilus", strategy_root=tmp_path)

        assert result.ok is False
        assert any("[MISSING]" in f and "config.yaml" in f for f in result.failures)

    def test_invalid_yaml_reported(self, tmp_path):
        """validate_startup returns ok=False with [INVALID] YAML failure for broken YAML."""
        strategy_dir = tmp_path / "mystrat"
        strategy_dir.mkdir()
        (strategy_dir / "config.yaml").write_text("{bad: [yaml: broken")

        result = validate_startup("mystrat", "nautilus", strategy_root=tmp_path)

        assert result.ok is False
        assert any("[INVALID]" in f and "YAML" in f for f in result.failures)

    def test_empty_config_reported(self, tmp_path):
        """validate_startup returns ok=False with [INVALID] empty failure for empty config.yaml."""
        strategy_dir = tmp_path / "mystrat"
        strategy_dir.mkdir()
        (strategy_dir / "config.yaml").write_text("")  # 0 bytes / empty

        result = validate_startup("mystrat", "nautilus", strategy_root=tmp_path)

        assert result.ok is False
        assert any("[INVALID]" in f and "empty" in f.lower() for f in result.failures)

    def test_multiple_failures_collected(self, tmp_path):
        """validate_startup collects all failures in one pass (non-existent strategy_root)."""
        non_existent_root = tmp_path / "does_not_exist"
        # Neither the root nor the strategy dir exists

        result = validate_startup("some_strategy", "nautilus", strategy_root=non_existent_root)

        assert result.ok is False
        assert len(result.failures) >= 1

    def test_unknown_platform_reported(self, tmp_path):
        """validate_startup returns ok=False with [INVALID] platform failure for unknown platform."""
        result = validate_startup("x", "invalid_platform", strategy_root=tmp_path)

        assert result.ok is False
        assert any("[INVALID]" in f and "platform" in f.lower() for f in result.failures)


class TestAbortOnFailure:
    """Tests for abort_on_failure() — RTF-02."""

    def test_abort_exits_nonzero(self):
        """abort_on_failure calls sys.exit(1) when validation failed."""
        result = ValidationResult(ok=False, failures=["[MISSING] dir"])

        with pytest.raises(SystemExit) as exc_info:
            abort_on_failure(result)

        assert exc_info.value.code == 1

    def test_abort_prints_to_stderr(self, capsys):
        """abort_on_failure prints failure messages to stderr."""
        result = ValidationResult(
            ok=False, failures=["[MISSING] Strategy directory not found: /some/path"]
        )

        with pytest.raises(SystemExit):
            abort_on_failure(result)

        captured = capsys.readouterr()
        assert "[MISSING]" in captured.err

    def test_abort_noop_on_ok(self):
        """abort_on_failure does nothing (no SystemExit) when validation passed."""
        result = ValidationResult(ok=True, failures=[])

        # Must NOT raise SystemExit
        abort_on_failure(result)


class TestLogProvenance:
    """Tests for log_provenance() — RTF-03."""

    def test_provenance_log_fields(self, caplog):
        """log_provenance emits INFO log with all required provenance fields."""
        with caplog.at_level(logging.INFO):
            log_provenance(
                "supertrend",
                "nautilus",
                "trend/supertrend/config.yaml",
                "sandbox",
            )

        assert "[provenance]" in caplog.text
        assert "strategy=supertrend" in caplog.text
        assert "platform=nautilus" in caplog.text
        assert "config=trend/supertrend/config.yaml" in caplog.text
        assert "mode=sandbox" in caplog.text
        assert "version=" in caplog.text

    def test_provenance_unknown_version(self, caplog):
        """log_provenance gracefully handles missing engine (returns version string or 'unknown')."""
        with caplog.at_level(logging.INFO):
            log_provenance("x", "hummingbot", "some/path/config.yaml", "live")

        # version= must appear — value is either a version string or "unknown"
        assert "version=" in caplog.text
