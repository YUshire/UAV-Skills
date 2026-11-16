"""Pure nominal battery-reserve arithmetic; callers validate supplied numbers."""

RESERVE_BATTERY_PERCENT = 20


def available_range_m(flight_remain: float, battery_level: float) -> float:
    return flight_remain * (1 - RESERVE_BATTERY_PERCENT / battery_level)


def available_time_s(max_endurance: float, battery_level: float) -> float:
    """max_endurance is full-charge endurance in minutes, battery_level a percent."""
    return max_endurance * 60 * (battery_level - RESERVE_BATTERY_PERCENT) / 100
