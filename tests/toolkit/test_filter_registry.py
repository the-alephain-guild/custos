# tests/test_filter_registry.py
"""Tests for filter registry."""

import pytest
from custos_toolkit.filters import (
    BaseFilter,
    clear_registry,
    create_filter,
    is_filter_registered,
    list_filters,
    register_filter,
)
from custos_toolkit.protocols import FilterResult


class TestFilterRegistry:
    """Tests for filter registry functions."""

    def setup_method(self):
        """Clear registry before each test."""
        clear_registry()

    def teardown_method(self):
        """Clear registry after each test."""
        clear_registry()

    def test_register_and_create_filter(self):
        """Should register and create filter by name."""

        @register_filter("test")
        class TestFilter(BaseFilter):
            name = "test"

            def update(self, bar):
                pass

            def check(self, bar):
                return FilterResult.allow()

        assert is_filter_registered("test")
        assert "test" in list_filters()

        instance = create_filter("test", {})
        assert isinstance(instance, TestFilter)

    def test_create_unknown_filter_raises(self):
        """Should raise ValueError for unknown filter."""
        with pytest.raises(ValueError, match="Unknown filter"):
            create_filter("nonexistent", {})

    def test_duplicate_registration_raises(self):
        """Should raise ValueError on duplicate registration."""

        @register_filter("dupe")
        class Filter1(BaseFilter):
            name = "dupe"

            def update(self, bar):
                pass

            def check(self, bar):
                return FilterResult.allow()

        with pytest.raises(ValueError, match="already registered"):

            @register_filter("dupe")
            class Filter2(BaseFilter):
                name = "dupe"

                def update(self, bar):
                    pass

                def check(self, bar):
                    return FilterResult.allow()

    def test_list_filters_empty(self):
        """list_filters should return empty list when no filters registered."""
        assert list_filters() == []

    def test_config_passed_to_filter(self):
        """Config should be passed to filter constructor."""

        @register_filter("configtest")
        class ConfigFilter(BaseFilter):
            name = "configtest"

            def update(self, bar):
                pass

            def check(self, bar):
                return FilterResult.allow()

        config = {"key": "value", "num": 42}
        instance = create_filter("configtest", config)
        assert instance.config == config
