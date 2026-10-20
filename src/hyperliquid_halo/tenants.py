"""The HALO tenants this repo ships to, and what is kept apart per tenant.

Everything that leaves this repo for HALO is tied to one tenant, and a part
sent to the wrong tenant, or recorded against the wrong one, cannot be
undone from the uploader side. So each tenant owns three things that never
mix:

* **An output root**, ``output/<TENANT>/``. Every window exported for a
  tenant goes into a folder under it (``<feed>_<first day>_<last day>``),
  and that folder carries the export ledger and the ``upload_<TENANT>.done``
  file of that window. :func:`default_out_dir` builds the path; the window
  commands use it whenever ``--out-dir`` is not given.
* **A part-name prefix.** Solidus names upload files
  ``<org>_<fileType>_<DDMMYYYY>_part<N>.csv``. HLRESEARCH keeps ``sdny``,
  the prefix of the production pipeline's daily files already on that
  tenant; a new tenant gets its own name in lower case, so the source file
  recorded in HALO (``orig_file``) says which tenant a part was made for.
* **A ledger document**, ``docs/tenants/<TENANT>/HALO_UPLOAD_LEDGER.md``,
  recording what was sent, when, and how it verified. The ``docs/tenants/``
  README indexes them.

The API key of a tenant lives in the ``halo-upload`` skill's ``.env`` as
``HALO_API_KEY_<TENANT>``, never here. ClickHouse knows a tenant as
``exchange`` (``strict_events``) and ``solidus_client`` (raw tables); the
coverage check uses the same upper-case name.

A tenant that is not registered here can still be named on the command
line (``--tenant``); it gets the generic defaults from
:func:`tenant_by_name`. Register it once it has shipped anything, so the
prefix and the ledger location are fixed in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .feeds import Feed

DEFAULT_OUTPUT_ROOT = Path("output")
"""Folder (relative to the working directory) that holds one sub-folder per tenant."""

LEDGER_DOCS_ROOT = Path("docs") / "tenants"
"""Folder holding one sub-folder per tenant with its upload ledger."""

LEDGER_FILENAME = "HALO_UPLOAD_LEDGER.md"

_TENANT_RE = re.compile(r"^[A-Z0-9_]+$")


@dataclass(frozen=True)
class Tenant:
    """One HALO tenant as the window commands see it.

    Attributes:
        name: Tenant name in upper case: the ``--tenant`` value, the
            ``HALO_API_KEY_<TENANT>`` suffix in the halo-upload skill's
            ``.env``, and the ``exchange`` / ``solidus_client`` value in
            ClickHouse.
        file_prefix: Prefix of every part name exported for the tenant.
        aliases: Other names the tenant goes by in Solidus tooling.
        description: One line on what the tenant is for.
    """

    name: str
    file_prefix: str
    aliases: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not _TENANT_RE.match(self.name):
            raise ValueError(f"Tenant name must match {_TENANT_RE.pattern}, got {self.name!r}")
        if not self.file_prefix or not re.match(r"^[A-Za-z0-9]+$", self.file_prefix):
            raise ValueError(
                f"file_prefix must be letters and digits only (Solidus file naming), "
                f"got {self.file_prefix!r}"
            )

    @property
    def output_root(self) -> Path:
        """``output/<TENANT>/``: where every window of this tenant is exported."""
        return DEFAULT_OUTPUT_ROOT / self.name

    @property
    def ledger_doc(self) -> Path:
        """``docs/tenants/<TENANT>/HALO_UPLOAD_LEDGER.md``: the tenant's upload ledger."""
        return LEDGER_DOCS_ROOT / self.name / LEDGER_FILENAME


HLRESEARCH = Tenant(
    name="HLRESEARCH",
    file_prefix="sdny",
    aliases=("UAT_HYPERLIQUID",),
    description=(
        "Hyperliquid research tenant (uat-eu-central-1); the production Snowflake pipeline "
        "delivers there daily with sdny_ file names, so this repo keeps that prefix."
    ),
)
"""The first tenant: full-market executions for several 2026 windows plus the xyz orders."""

HLSTRATEGIESTEST = Tenant(
    name="HLSTRATEGIESTEST",
    file_prefix="hlstrategiestest",
    description=(
        "Hyperliquid strategies test tenant; receives its own copy of the executions feed, "
        "starting with October 10 to 19, 2026. Nothing shared with HLRESEARCH."
    ),
)
"""The second tenant, set up on 2026-10-20."""

TENANTS: dict[str, Tenant] = {tenant.name: tenant for tenant in (HLRESEARCH, HLSTRATEGIESTEST)}
"""Registered tenants by upper-case name."""


def tenant_by_name(name: str) -> Tenant:
    """Look a tenant up by name, or describe an unregistered one with generic defaults.

    Args:
        name: Tenant name in any case (``hlresearch`` and ``HLRESEARCH`` are
            the same tenant).

    Returns:
        The registered :class:`Tenant`, or for an unregistered name a
        :class:`Tenant` whose ``file_prefix`` is the name in lower case.

    Raises:
        ValueError: If the name is not letters, digits and underscores.

    Example:
        >>> tenant_by_name("hlresearch").file_prefix
        'sdny'
        >>> tenant_by_name("NEWTENANT").file_prefix
        'newtenant'
    """
    cleaned = name.strip().upper()
    if not _TENANT_RE.match(cleaned):
        raise ValueError(f"Tenant must match {_TENANT_RE.pattern}, got {name!r}")
    known = TENANTS.get(cleaned)
    if known is not None:
        return known
    return Tenant(name=cleaned, file_prefix=cleaned.lower().replace("_", ""),
                  description="unregistered tenant (generic defaults)")


def default_out_dir(
    tenant: Tenant,
    feed: Feed,
    start: date,
    end_exclusive: date,
    *,
    root: Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    """Folder a window of one feed is exported to for ``tenant``.

    The layout is ``<root>/<TENANT>/<feed>_<YYYYMMDD first day>_<YYYYMMDD
    last day>``, so windows of different tenants can never share a folder,
    a ledger or a done-file, and the two feeds of one window sit side by
    side.

    Args:
        tenant: The tenant the window is for.
        feed: Executions or orders.
        start: First transact date (inclusive).
        end_exclusive: Day after the last transact date.
        root: Folder holding the per-tenant sub-folders.

    Returns:
        The folder path (not created).

    Raises:
        ValueError: If ``end_exclusive`` is not after ``start``.

    Example:
        >>> from hyperliquid_halo.feeds import EXECUTIONS
        >>> default_out_dir(HLSTRATEGIESTEST, EXECUTIONS, date(2026, 10, 10), date(2026, 10, 20))
        PosixPath('output/HLSTRATEGIESTEST/executions_20261010_20261019')
    """
    if end_exclusive <= start:
        raise ValueError(f"end_exclusive ({end_exclusive}) must be after start ({start})")
    last = end_exclusive - timedelta(days=1)
    return root / tenant.name / f"{feed.name}_{start:%Y%m%d}_{last:%Y%m%d}"


__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "HLRESEARCH",
    "HLSTRATEGIESTEST",
    "LEDGER_DOCS_ROOT",
    "TENANTS",
    "Tenant",
    "default_out_dir",
    "tenant_by_name",
]
