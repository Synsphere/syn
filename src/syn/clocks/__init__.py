"""Read-only UTC awareness; never disciplines or sets the host clock."""
from .config import ClockConfig
from .types import ClockReading

__all__ = ["ClockConfig", "ClockReading"]
