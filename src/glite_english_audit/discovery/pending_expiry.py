"""Expire the inventory discovery leaves behind before a run exists.

Discovery runs before the user has chosen anything, so it has no run to write
into and leaves its result in a pending location. That file maps every source
instance to where the user's data actually lives on this machine: on the
owner's own laptop, sixty absolute paths under their home directory.

Inside a run that map expires with the run under the 30-day rule in
specification 3.6. The pending copy had no owner and no expiry, so a user who
ran discovery and then closed the conversation left it there permanently. That
is the same class of defect as the run directory orphaned outside the
checkout: private data with nothing left pointing at it and nothing scheduled
to remove it.

The window is deliberately much shorter than a run's. A pending inventory
exists to carry an answer across a few minutes of setup conversation, not to
be a cache. Anything older is both a privacy liability and probably wrong:
sources appear and disappear, and starting a run from a week-old map means
snapshotting paths that may no longer be what they were.

Limits worth being honest about. This tool has no daemon, so expiry runs only
when someone invokes it: discovery checks on the way in, and ``start_run``
refuses a stale inventory rather than auditing from an outdated map. A user
who runs discovery once and never opens the tool again keeps that file until
they do. What is guaranteed is narrower and still worth having — stale paths
can never be *used*, and the file is removed the next time the tool runs.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from glite_english_audit.artifacts.io import read_model
from glite_english_audit.discovery.inventory import PrivateInventory
from glite_english_audit.paths import pending_inventory_dir

INVENTORY_NAME = "source-inventory.json"

PENDING_INVENTORY_MAX_AGE_DAYS: int = 7
"""How long a pending inventory may sit before it is deleted unread."""


def pending_inventory_path(*, inventory_dir: Path | None = None) -> Path:
    """Where discovery leaves its inventory before a run adopts it."""
    base = inventory_dir if inventory_dir is not None else pending_inventory_dir()
    return base / INVENTORY_NAME


def is_stale(inventory: PrivateInventory, *, now: datetime | None = None) -> bool:
    """Whether this inventory is older than the pending window.

    An inventory without ``created_at`` was written before expiry existed and
    counts as stale: the safe reading of an unknown age is "too old", because
    the alternative keeps private paths indefinitely on the strength of a
    missing field.
    """
    if inventory.created_at is None:
        return True
    moment = now if now is not None else datetime.now(tz=UTC)
    created = inventory.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return moment - created > timedelta(days=PENDING_INVENTORY_MAX_AGE_DAYS)


def expire_pending_inventory(
    *, inventory_dir: Path | None = None, now: datetime | None = None
) -> bool:
    """Delete the pending inventory when it is stale. True when it was deleted.

    Unreadable or invalid files are deleted too. A file at this path that does
    not parse as an inventory is not something to preserve for inspection: it
    is a file of unknown content sitting where private paths belong.
    """
    target = pending_inventory_path(inventory_dir=inventory_dir)
    if not target.is_file():
        return False
    try:
        inventory = read_model(target, PrivateInventory)
    except Exception:
        target.unlink()
        return True
    if not is_stale(inventory, now=now):
        return False
    target.unlink()
    return True
