"""Fixtures for tests using Home Assistant's integration harness."""

import pytest


@pytest.fixture(autouse=True)
def enable_custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load this custom integration in tests."""
    yield
