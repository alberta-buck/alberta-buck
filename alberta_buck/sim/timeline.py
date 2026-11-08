"""Absolute runner timestamps; work between ticks never adds model days."""

DAY_SECONDS = 86400


def tick_timestamp(epoch, day, tick, ticks_per_day, day_step=1):
    """Timestamp relative to the first tick of model day zero.

    Pre: integer nonnegative origin/day, valid tick and at least one minute
    per tick. Post: identical tick offsets on different days differ by the
    exact whole-day interval, including non-divisor tick counts. day_step
    is the existing batch runner's period, not an extra accumulating delay.
    """
    if (any(type(v) is not int for v in (epoch, day, tick, ticks_per_day, day_step)) or
            min(epoch, day) < 0 or day_step < 1 or
            not 0 < ticks_per_day <= day_step * 1440 or not 0 <= tick < ticks_per_day):
        raise ValueError("invalid simulation clock origin/day/tick")
    return epoch + day * DAY_SECONDS + tick * day_step * DAY_SECONDS // ticks_per_day
