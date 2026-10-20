"""The two HALO feeds this package ships, and what differs between them.

The window commands (``export-window``, ``upload-window``, ``check-coverage``)
are the same procedure for executions and for orders; only four things
change: which exporter runs, the HALO file type that names the parts and is
sent to the uploader, the ``event_type`` under which HALO stores the rows in
``strict_events``, and the raw table the loader fills first. A :class:`Feed`
carries those, so no command hardcodes the executions values.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    """One HALO feed as the window commands see it.

    Attributes:
        name: CLI name (``executions`` or ``orders``).
        file_type: HALO file type; part of every part name and the value
            passed to the uploader.
        event_type: ``strict_events.event_type`` of the ingested rows.
        raw_table: ClickHouse raw table the loader fills before
            ``strict_events``; it carries the source file per row.
    """

    name: str
    file_type: str
    event_type: str
    raw_table: str


EXECUTIONS = Feed(
    name="executions",
    file_type="LINKED_PRIVATE_EXECUTION_V2",
    event_type="EXECUTION",
    raw_table="raw_realtime_matched_executions",
)
"""The v2.1 executions feed, the live route used for every window so far."""

ORDERS = Feed(
    name="orders",
    file_type="PRIVATE_ORDER_V2",
    event_type="ORDER",
    raw_table="raw_realtime_orders",
)
"""The v2.1 orders feed (HALO's ``PRIVATE_ORDER_V2`` covers v2 and v2.1 order files)."""

FEEDS: dict[str, Feed] = {feed.name: feed for feed in (EXECUTIONS, ORDERS)}
"""Feeds by CLI name."""


def feed_by_name(name: str) -> Feed:
    """Look a feed up by its CLI name.

    Args:
        name: ``executions`` or ``orders`` (case-insensitive).

    Returns:
        The matching :class:`Feed`.

    Raises:
        ValueError: If the name is not a known feed.
    """
    try:
        return FEEDS[name.lower()]
    except KeyError:
        raise ValueError(f"Unknown feed {name!r}; expected one of {', '.join(FEEDS)}") from None
