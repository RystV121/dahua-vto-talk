"""Shared fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_local_sockets(socket_enabled):
    """The simulated VTO listens on 127.0.0.1; let tests reach it."""
    yield


@pytest.fixture(autouse=True)
def _enable_custom_integrations(request):
    """Load custom_components/ in Home Assistant tests."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
    yield
