"""Shared issue #639 clock for the retained report and holding-news tests."""

from datetime import datetime

from app.core.timezones import ET

NOW = datetime(2026, 10, 3, 19, tzinfo=ET)
