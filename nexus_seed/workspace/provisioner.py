"""WorkspaceProvisioner — put the granted resources where the Agent will find them.

This is what lets an unmodified Agent work under a permission model it knows
nothing about: it is handed a directory that already contains exactly what it
was granted, so "which files may I open" never has to be a question it asks.

    <workspace>/            the task's own scratch space, always writable
      resources/            what was granted, one file per grant
      RESOURCES.md          the same list, readable by a human or an LLM
      .nexus-seed/manifest.json   the machine-readable manifest

A grant arrives one of two ways, and the choice is the caller's:

**By reference** — nothing is copied.  The manifest carries the resolved host
path and the Agent reads or writes the original in place.  This is the ordinary
case: most files a task consults do not want duplicating, a write meant to land
in the real file should just land there, and a directory cannot sensibly be
duplicated at all.  What bounds it is the authorized root, the same one that
bounds everything else — not the absence of a path.

**By copy** — materialised into ``resources/`` inside the workspace.  It costs
a copy, and it buys the one thing a reference cannot: an Agent may edit the
file while the original stays untouched.  A copy is never a symlink, because a
symlink would let a directory grant be walked out of and would tie the Agent to
this host.

The guarantee a read *copy* carries is precisely this: the Agent is given a
copy, and :meth:`collect` never carries a read grant back, so whatever the
Agent does to that file the original is untouched.  A read-only copy is *also*
written with a read-only file mode, but that is a signal rather than the
boundary — a process running as root, or as the file's owner, can write it
anyway.  Do not read the mode as the protection; the protection is that
nothing returns.  A read-write copy is carried back by :meth:`collect`, which
is the one moment edits return to where they came from.

A read *reference* carries no such guarantee and does not pretend to: the Agent
is pointed at the real file, and read-only is the access it was asked to keep.
Use a copy when the original has to be safe from the Agent, and a reference
when it does not.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (
    SCHEME_FILE,
    SCHEME_KNOWLEDGE,
    SCHEME_RESOURCE,
    AccessMode,
    Delivery,
    ResourceGrant,
    WorkspaceManifest,
)
from .policy import GrantPolicy, GrantRefused

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..knowledge.ledger import KnowledgeLedger
    from ..resources.service import ResourceService

logger = logging.getLogger("nexus_seed.workspace.provisioner")

#: Where granted resources live inside a task workspace.
RESOURCES_DIR = "resources"

#: Where the machine-readable manifest lives.
MANIFEST_PATH = ".nexus-seed/manifest.json"

#: A human- and LLM-readable index, for an Agent that reads files but not
#: A2A metadata.  Same content as the manifest; no second source of truth.
README_PATH = "RESOURCES.md"

_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_READ_WRITE = _READ_ONLY | stat.S_IWUSR


class WorkspaceProvisioner:
    """Builds one task workspace from a set of grants."""

    def __init__(
        self,
        policy: GrantPolicy,
        *,
        resources: "ResourceService | None" = None,
        ledger: "KnowledgeLedger | None" = None,
    ) -> None:
        self.policy = policy
        self.resources = resources or policy.resources
        self.ledger = ledger or policy.ledger

    # --- provisioning -------------------------------------------------------

    def provision(
        self, workspace: str | Path, grants: list[ResourceGrant]
    ) -> WorkspaceManifest:
        """Create ``workspace`` and materialise every still-permitted grant.

        Re-provisioning is how a task continues after being given something
        more: the workspace is kept, previously provisioned resources are
        refreshed, and whatever the Agent wrote in its own scratch space is
        left alone.
        """
        root = Path(workspace).expanduser().resolve()
        resources_dir = root / RESOURCES_DIR
        resources_dir.mkdir(parents=True, exist_ok=True)
        (root / Path(MANIFEST_PATH).parent).mkdir(parents=True, exist_ok=True)

        entries: list[dict] = []
        used_names: set[str] = set()
        for grant in self.policy.permitted(grants):
            try:
                if grant.delivery is Delivery.REFERENCE:
                    path = self._reference(grant)
                else:
                    name = self._unique_name(grant, used_names)
                    self._materialise(grant, resources_dir / name)
                    path = f"{RESOURCES_DIR}/{name}"
            except (GrantRefused, OSError, ValueError) as exc:
                # One unreadable grant must not cost the task the rest of them;
                # the Agent is told what it has, and this is simply not in it.
                logger.warning("could not provision %s: %s", grant.uri, exc)
                used_names.discard(self._name_for(grant))
                continue
            entries.append(
                {
                    "uri": grant.uri,
                    "path": path,
                    "access": grant.access.value,
                    "delivery": grant.delivery.value,
                    "reason": grant.reason,
                }
            )

        manifest = WorkspaceManifest(workspace=str(root), entries=entries)
        self._write_manifest(root, manifest)
        return manifest

    def collect(self, workspace: str | Path, grants: list[ResourceGrant]) -> list[str]:
        """Carry writable *copies* back to where they came from.

        Called when a task's work is accepted.  Only writable file grants
        delivered by copy move: a read grant was a copy on purpose, a
        reference grant already wrote to the original so there is nothing to
        carry, and a Resource or Knowledge grant has no writable original to
        return to.
        """
        root = Path(workspace).expanduser().resolve()
        written: list[str] = []
        for grant in grants:
            if not grant.access.writable or grant.scheme != SCHEME_FILE:
                continue
            if grant.delivery is Delivery.REFERENCE:
                continue
            source = root / RESOURCES_DIR / self._name_for(grant)
            if not source.is_file():
                continue
            try:
                destination = self.policy.scope.resolve(grant.target, write=True)
                shutil.copy2(source, destination)
            except Exception as exc:  # noqa: BLE001 - report, never lose the rest
                logger.warning("could not write %s back: %s", grant.uri, exc)
                continue
            written.append(grant.uri)
        return written

    # --- reference ----------------------------------------------------------

    def _reference(self, grant: ResourceGrant) -> str:
        """Return the host path a referenced grant points at.

        Only ``file:`` can be referenced.  A Resource version and a Knowledge
        object are records in a database, not files on this host, so there is
        no path to hand over and asking for one is a mistake worth naming.
        """
        if grant.scheme != SCHEME_FILE:
            raise GrantRefused(
                f"{grant.scheme}: can only be delivered as a copy; it is a "
                "record, not a file on this host"
            )
        if self.policy.scope is None:
            raise GrantRefused("no authorized file root is configured")
        resolved = Path(self.policy.scope.resolve(grant.target, write=grant.access.writable))
        if not resolved.exists():
            raise GrantRefused(f"{grant.uri} no longer exists")
        return str(resolved)

    # --- per-scheme materialisation -----------------------------------------

    def _materialise(self, grant: ResourceGrant, target: Path) -> None:
        if grant.scheme == SCHEME_FILE:
            self._copy_file(grant, target)
        elif grant.scheme == SCHEME_RESOURCE:
            self._write_resource(grant, target)
        elif grant.scheme == SCHEME_KNOWLEDGE:
            self._write_knowledge(grant, target)
        else:  # pragma: no cover - policy refuses these first
            raise GrantRefused(f"unsupported resource scheme {grant.scheme!r}")
        self._apply_mode(target, grant.access)

    def _copy_file(self, grant: ResourceGrant, target: Path) -> None:
        source = self.policy.scope.resolve(grant.target)
        if not Path(source).is_file():
            raise GrantRefused(f"{grant.uri} is not a readable file")
        # copyfile, not copy2: the source's mode must not override the mode the
        # access decides, or a read grant of a writable file would stay writable.
        shutil.copyfile(source, target)

    def _write_resource(self, grant: ResourceGrant, target: Path) -> None:
        if self.resources is None:
            raise GrantRefused("the Resource store is not available")
        resource = self.resources.get_resource_by_uri(grant.target)
        if resource is None:
            raise GrantRefused(f"no Resource by uri {grant.target!r}")
        version = self.resources.get_current_version(resource.id)
        if version is None or not version.locator:
            raise GrantRefused(f"Resource {grant.target!r} has no readable version")
        target.write_bytes(self.resources.read_bytes(version.locator))

    def _write_knowledge(self, grant: ResourceGrant, target: Path) -> None:
        if self.ledger is None:
            raise GrantRefused("the Knowledge Ledger is not available")
        item = self.ledger.head(grant.target)
        if item is None:
            raise GrantRefused(f"no Knowledge by id {grant.target!r}")
        value = item.content.value
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, indent=2)
        )
        target.write_text(text, encoding="utf-8")

    @staticmethod
    def _apply_mode(target: Path, access: AccessMode) -> None:
        os.chmod(target, _READ_WRITE if access.writable else _READ_ONLY)

    # --- naming and manifest -------------------------------------------------

    @staticmethod
    def _name_for(grant: ResourceGrant) -> str:
        """The file name one grant appears under, before uniquifying."""
        if grant.name:
            return Path(grant.name).name
        tail = grant.target.rstrip("/").rsplit("/", 1)[-1]
        return tail or grant.target.replace(":", "_") or "resource"

    def _unique_name(self, grant: ResourceGrant, used: set[str]) -> str:
        """A name no other grant in this workspace already took."""
        base = self._name_for(grant)
        name, index = base, 1
        while name in used:
            stem, dot, suffix = base.partition(".")
            name = f"{stem}-{index}{dot}{suffix}"
            index += 1
        used.add(name)
        return name

    def _write_manifest(self, root: Path, manifest: WorkspaceManifest) -> None:
        (root / MANIFEST_PATH).write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (root / README_PATH).write_text(_readme(manifest), encoding="utf-8")


def _readme(manifest: WorkspaceManifest) -> str:
    """The same manifest as prose, for an Agent that only reads files."""
    lines = [
        "# Resources provided for this task",
        "",
        "These were granted by NEXUS SEED for this task. Anything not listed",
        "here was not granted: ask for it with a NEED_RESOURCE message rather",
        "than looking for it elsewhere on this machine.",
        "",
        "This workspace is yours for this task. Work in it, and in the paths",
        "listed below.",
        "",
    ]
    if not manifest.entries:
        lines.append("No resources beyond this workspace were granted.")
    else:
        lines.append("| path | access | kind | why |")
        lines.append("| --- | --- | --- | --- |")
        for entry in manifest.entries:
            access = "read-only" if entry["access"] == AccessMode.READ.value else "read-write"
            referenced = entry.get("delivery") == Delivery.REFERENCE.value
            kind = "in place" if referenced else "copy"
            lines.append(
                f"| `{entry['path']}` | {access} | {kind} | {entry.get('reason', '')} |"
            )
        lines.extend(
            [
                "",
                "**in place** — the real file or directory, at the path shown.",
                "Reading it reads the original; if it is read-write, writing it",
                "changes the original, and that is what it is for.",
                "",
                "**copy** — a copy inside this workspace, at the relative path",
                "shown. A read-only copy changes nothing anywhere no matter what",
                "you do to it. A read-write copy is carried back to where it came",
                "from when your work is accepted.",
            ]
        )
    return "\n".join(lines) + "\n"


__all__ = ["MANIFEST_PATH", "README_PATH", "RESOURCES_DIR", "WorkspaceProvisioner"]
