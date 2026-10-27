"""Backwards-compatible re-export of the per-link day-event helpers.

The filtering logic moved to :mod:`calendar_links` in plan step 4 when attendee
matching (F5) was added. This module stays as the import path the scheduler and
dispatcher already use (``from .day_events import profile_day_events``) so those
call sites did not have to change. New code should import from
:mod:`calendar_links` directly.
"""

from __future__ import annotations

from .calendar_links import filter_for_link, profile_day_events

__all__ = ["filter_for_link", "profile_day_events"]
