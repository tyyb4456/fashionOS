"""
FashionOS Seasonal Demand Calendar
===================================
Pakistani fashion demand isn't flat across the year. This module gives the
Inventory Agent (and anything downstream) a structured, programmatic read on
"what's the demand environment right now" instead of leaving seasonality to
LLM vibes buried in a skill prompt.

Eid dates are lunar (Hijri) and shift ~10-11 days earlier every Gregorian year.
CONFIRMED dates are hardcoded for years we know; ESTIMATED dates for future
years are extrapolated and flagged as such — re-confirm via Ruet-e-Hilal
Committee moon-sighting announcement closer to the date, actual date can
shift ±1 day.

>>> UPDATE EID_CALENDAR EVERY YEAR <<< — add next year's confirmed dates each
January/June once the committee announces them.
"""

from datetime import date
from typing import NamedTuple


class SeasonalEvent(NamedTuple):
    label:             str
    peak_date:         date
    demand_multiplier: float   # applied to velocity forecast at the peak
    ramp_up_days:      int     # how many days before peak_date demand starts climbing
    confirmed:         bool    # False = estimated/extrapolated Hijri date


# Confirmed via Ruet-e-Hilal Committee announcements.
EID_CALENDAR: list[SeasonalEvent] = [
    SeasonalEvent("eid_ul_fitr_2026", date(2026, 3, 21), 1.45, 21, confirmed=True),
    SeasonalEvent("eid_ul_adha_2026", date(2026, 5, 27), 1.25, 14, confirmed=True),
    # Hijri year shifts ~10-11 days earlier each Gregorian year — estimate only.
    SeasonalEvent("eid_ul_fitr_2027", date(2027, 3, 10), 1.45, 21, confirmed=False),
    SeasonalEvent("eid_ul_adha_2027", date(2027, 5, 17), 1.25, 14, confirmed=False),
]

# Fixed Gregorian-calendar seasons from the fashion_inventory skill.
# (start_month, end_month, label, demand_multiplier) — wraps year boundary if start > end.
GREGORIAN_SEASONS: list[tuple[int, int, str, float]] = [
    (5, 7,  "summer_peak",           1.15),
    (11, 1, "winter_wedding_season", 1.10),
]


def current_seasonal_context(today: date | None = None) -> dict:
    """
    Returns the seasonal demand context for `today` (defaults to real today).

    {
      "season_label":         str,
      "demand_multiplier":    float,      # highest applicable multiplier right now
      "days_until_next_peak": int | None,
      "next_peak_label":      str | None,
      "next_peak_confirmed":  bool | None,
    }
    """
    today = today or date.today()
    active_multiplier = 1.0
    active_label = "off_season"
    upcoming: tuple[int, SeasonalEvent] | None = None

    # ── Eid ramp-up windows ─────────────────────────────────────────────────
    for ev in EID_CALENDAR:
        days_to_peak = (ev.peak_date - today).days

        if 0 <= days_to_peak <= ev.ramp_up_days:
            progress = 1 - (days_to_peak / ev.ramp_up_days)
            scaled = 1.0 + (ev.demand_multiplier - 1.0) * max(progress, 0.3)
            if scaled > active_multiplier:
                active_multiplier = round(scaled, 2)
                active_label = ev.label

        if days_to_peak >= 0 and (upcoming is None or days_to_peak < upcoming[0]):
            upcoming = (days_to_peak, ev)

    # ── Fixed Gregorian seasons ──────────────────────────────────────────────
    month = today.month
    for start, end, label, mult in GREGORIAN_SEASONS:
        in_season = (start <= month <= end) if start <= end else (month >= start or month <= end)
        if in_season and mult > active_multiplier:
            active_multiplier = mult
            active_label = label

    return {
        "season_label":         active_label,
        "demand_multiplier":    active_multiplier,
        "days_until_next_peak": upcoming[0] if upcoming else None,
        "next_peak_label":      upcoming[1].label if upcoming else None,
        "next_peak_confirmed":  upcoming[1].confirmed if upcoming else None,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(current_seasonal_context(), indent=2))