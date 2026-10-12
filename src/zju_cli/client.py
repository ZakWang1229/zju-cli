"""Compatibility import for early callers; business logic lives in services."""

from .services.platform import ZjuService as ZjuClient

__all__ = ["ZjuClient"]
