"""CapabilityRegistry — the system's model of what it can currently do.

Conceptually this is the other half of World State (spec §88–§89): world state
is what NEXUS SEED knows about the *outside*, the capability registry is what it
knows about *itself*.  They are kept apart because they answer different
questions and change for different reasons — enabling a capability is not a fact
about the world.

The registry is persistent and does no reasoning.  It answers "who provides X?"
and "what does definition Y provide?"; deciding which provider to use is the
matcher's job, and deciding what a piece of work needs is the work domain's.
"""

from __future__ import annotations

import logging

from .models import Capability, CapabilityRef, CapabilityRequirement

logger = logging.getLogger("nexus_seed.capabilities")


class CapabilityRegistry:
    """Reads and writes what the system can do, backed by SQLite."""

    def __init__(self, store) -> None:
        self.store = store

    # --- registration ------------------------------------------------------

    def register_capability(self, capability: Capability | str, **kwargs) -> Capability:
        """Declare a capability (idempotent by name + version)."""
        if isinstance(capability, str):
            capability = Capability(name=capability, **kwargs)
        stored = self.store.save(capability)
        logger.info("registered capability %s", stored.ref)
        return stored

    def declare(
        self,
        definition_name: str,
        definition_version: str,
        refs: tuple[CapabilityRef, ...] | list[CapabilityRef],
        *,
        replace: bool = True,
    ) -> list[CapabilityRef]:
        """Record that a definition provides ``refs``.

        Returns only the declarations that are **newly** available — a
        re-registered definition announces nothing (spec §83), which is what
        keeps ``capability_available`` from firing on every startup.

        A capability named here that does not exist yet is created, so a
        process can introduce a competence simply by claiming it.
        """
        # What this definition already provided, captured *before* rewriting
        # the relations — otherwise every restart would re-announce everything
        # and reconciliation would run on every startup.
        already = {
            c.key
            for c in self.store.capabilities_for(definition_name, definition_version)
        }
        if replace:
            self.store.unlink_all(definition_name, definition_version)

        newly_available: list[CapabilityRef] = []
        for ref in refs:
            capability = self.store.get(ref.name, ref.version)
            if capability is None:
                capability = self.store.save(Capability(name=ref.name, version=ref.version))
                logger.info("capability %s introduced by %s", ref, definition_name)
            self.store.link(definition_name, definition_version, capability.id)
            if (ref.name, ref.version) not in already:
                newly_available.append(ref)
        return newly_available

    def set_enabled(self, name: str, version: str, enabled: bool) -> bool:
        """Enable or disable a capability; return whether the state changed.

        Enabling counts as newly-available (spec §84), so blocked work gets
        another look.  Disabling only affects *future* matching — running
        processes are left alone (spec §43).
        """
        capability = self.store.get(name, version)
        if capability is None or capability.enabled == enabled:
            return False
        self.store.set_enabled(name, version, enabled)
        logger.info("capability %s:v%s -> %s", name, version, "enabled" if enabled else "disabled")
        return True

    # --- queries -----------------------------------------------------------

    def get_capability(self, name: str, version: str | None = None) -> Capability | None:
        """Return a capability; with no version, the preferred enabled one."""
        if version is not None:
            return self.store.get(name, version)
        versions = self.store.versions_of(name)
        return self._preferred(versions)

    def list_capabilities(self, *, enabled_only: bool = False) -> list[Capability]:
        """Return every declared capability."""
        return self.store.all(enabled_only=enabled_only)

    def get_capabilities_for_process(
        self, definition_name: str, definition_version: str
    ) -> list[Capability]:
        """Return what one definition declares it can do."""
        return self.store.capabilities_for(definition_name, definition_version)

    def get_processes_providing(
        self, name: str, version: str | None = None
    ) -> list[tuple[str, str]]:
        """Return the definitions that provide a capability."""
        return self.store.providers_of(name, version)

    def is_provided(self, requirement: CapabilityRequirement) -> bool:
        """Whether some process actually provides ``requirement``.

        Declaring a capability is not the same as being able to do it: a
        capability row with no provider is a *gap*, not an available
        competence.  This is the distinction between "we cannot do this at all"
        and "we cannot do it in one step" (spec §92), so it has to mean
        provision rather than mere existence.
        """
        for capability in self.store.versions_of(requirement.name):
            if not requirement.satisfied_by(capability):
                continue
            if self.store.providers_of(capability.name, capability.version):
                return True
        return False

    def provided_capability_names(self) -> set[str]:
        """Every capability name currently provided by some definition."""
        names: set[str] = set()
        for _, _, capability_id in self.store.all_links():
            capability = self.store.get_by_id(capability_id)
            if capability is not None and capability.enabled:
                names.add(capability.name)
        return names

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _preferred(capabilities: list[Capability]) -> Capability | None:
        """Pick one of several versions, deterministically (spec §44).

        Highest version wins, compared numerically when the strings are
        numeric and lexically otherwise — no SemVer solver, and no surprise
        from ``"10" < "9"``.
        """
        if not capabilities:
            return None
        return max(capabilities, key=lambda c: version_sort_key(c.version))


def version_sort_key(version: str):
    """A stable, numeric-aware sort key for a version string."""
    parts = version.split(".")
    numeric = all(part.isdigit() for part in parts) and bool(parts)
    if numeric:
        return (1, [int(part) for part in parts], "")
    return (0, [], version)


def as_refs(values) -> tuple[CapabilityRef, ...]:
    """Coerce a declaration list into ``CapabilityRef``s.

    Accepts bare names, ``(name, version)`` pairs, dicts or refs, so a process
    definition can declare capabilities in whatever form reads best.
    """
    refs: list[CapabilityRef] = []
    for value in values or ():
        if isinstance(value, CapabilityRef):
            refs.append(value)
        elif isinstance(value, str):
            refs.append(CapabilityRef(value))
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            refs.append(CapabilityRef(str(value[0]), str(value[1])))
        elif isinstance(value, dict):
            refs.append(
                CapabilityRef(value.get("name", ""), str(value.get("version", "1")))
            )
    return tuple(refs)
