"""Vendor-agnostic base exceptions shared by every source adapter.

Each vendor module (``home_assistant.py``, ``hydros.py``, ...) defines its own
authentication/transport exception pair for precise, vendor-specific messages,
but subclasses these shared bases so ``service.py`` can catch one pair of
types regardless of which source produced the failure.
"""

from __future__ import annotations


class SourceAuthenticationError(PermissionError):
    """Credentials were rejected by a source; not retryable without repair."""


class SourceTransportError(ConnectionError):
    """A source was unreachable or returned a transient server failure."""
