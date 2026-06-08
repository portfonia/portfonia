"""Shared timezone constants.

Using IANA zones (not fixed UTC offsets) so DST transitions are handled
correctly — e.g. US Eastern is UTC-5 in winter, UTC-4 in summer.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

# US Eastern — canonical market clock for FX rate_date boundaries (design §6.2).
ET = ZoneInfo("America/New_York")

# China Standard Time — A-share / mutual-fund NAV clock.
CST = ZoneInfo("Asia/Shanghai")

# Hong Kong — HKEX clock (no DST; constant UTC+8).
HKT = ZoneInfo("Asia/Hong_Kong")

# Market bucket → local clock, for stamping intraday capture trade_date.
MARKET_TZ = {"US": ET, "HK": HKT, "A-Share": CST}
