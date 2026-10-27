"""Who receives a copy of a notice (feature 7, plan §6.4).

A profile can be set to copy its notices to other profiles, typically a parent
being told what a child was told. :func:`copy_recipients` is a pure function
(plan §1 rule 3): no ``homeassistant`` import, ``now`` is a parameter. The
dispatcher fans the copies out, after the original, to each recipient's own push
script.

Rules:

* the fan-out comes from the *source* profile's ``copy_to`` only and copies are
  never copied again, so A→B plus B→A gives each of them exactly one copy;
* the recipient must exist, differ from the source and have notifications on;
* the notice kind must be one the source chose to copy (``copy_kinds``), except
  that ``cleanup`` is copied whenever any kind is, so a copied notice can be
  cleared;
* inside the *recipient's* quiet hours a copy is dropped, not held (there is no
  ledger for copies), except ``critical`` and ``cleanup``, which always pass;
* a recipient listed twice gets one copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .models import NotificationIntent, ProfileConfig
from .notification_policy import KIND_CLEANUP, KIND_CRITICAL, in_quiet_hours

# Kinds that reach a recipient even inside their quiet hours.
_ALWAYS_PASS = frozenset((KIND_CRITICAL, KIND_CLEANUP))


def copy_recipients(
    intent: NotificationIntent,
    source: ProfileConfig,
    profiles: Mapping[str, ProfileConfig],
    now: datetime,
) -> list[ProfileConfig]:
    """The profiles that get a copy of ``intent`` (sent by ``source``)."""
    # A cleanup clears a copied notice from the recipient's phone, so it follows
    # any copy that was sent, whether or not "cleanup" itself is ticked.
    if intent.kind not in source.copy_kinds and not (
        intent.kind == KIND_CLEANUP and source.copy_kinds
    ):
        return []
    recipients: list[ProfileConfig] = []
    seen: set[str] = set()
    for recipient_id in source.copy_to:
        if recipient_id in seen or recipient_id == source.id:
            continue
        seen.add(recipient_id)
        recipient = profiles.get(recipient_id)
        if recipient is None or not recipient.notifications_enabled:
            continue
        if intent.kind not in _ALWAYS_PASS and in_quiet_hours(
            now, recipient.quiet_start, recipient.quiet_end
        ):
            continue
        recipients.append(recipient)
    return recipients
