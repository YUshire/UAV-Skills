"""Pure nominal battery-reserve and default-altitude arithmetic."""

RESERVE_BATTERY_PERCENT = 20
DEFAULT_REF_ALT_M = 50.0


def effective_altitude_m(max_altitude: float, flight_alt: float | None = None,
                         ref_alt_m: float = DEFAULT_REF_ALT_M) -> float:
    """Data-construction altitude rule: min(ref_alt_m, max_altitude) unless explicitly specified."""
    if flight_alt is not None:
        return flight_alt
    return min(ref_alt_m, max_altitude)


def available_range_m(flight_remain: float, battery_level: float,
                      reserve_percent: float = RESERVE_BATTERY_PERCENT) -> float:
    return flight_remain * (1 - reserve_percent / battery_level)


def available_time_s(max_endurance: float, battery_level: float,
                     reserve_percent: float = RESERVE_BATTERY_PERCENT) -> float:
    """max_endurance is full-charge endurance in minutes, battery_level a percent."""
    return max_endurance * 60 * (battery_level - reserve_percent) / 100
