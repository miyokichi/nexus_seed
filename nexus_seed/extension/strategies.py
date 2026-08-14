"""Extension strategies — the order to try them in, and what each would cost.

Three deterministic tables, and each of them is a decision this phase makes
once so that nothing later has to make it by judgement:

* :data:`STRATEGY_ORDER` — **reuse before construction** (Invariant 88).  Not a
  heuristic: a system that can write new code will always find writing new code
  easier than discovering what it already has, and that tendency compounds.
* :data:`STRATEGY_RISK` — what kind of thing each strategy touches (spec §40).
  Classification is code, not a model's opinion.
* :data:`STRATEGY_PERMISSIONS` — what *constructing* the extension would need.
  Declaring a permission on a proposal grants nothing (spec §77); this table
  exists so a proposal cannot under-declare its way past the risk gate, the same
  rule Phase 3C applies to actions.

Unknown is never treated as safe (spec §41): an unrecognised strategy or
component type is CRITICAL, which under the default policy means it does not
proceed.
"""

from __future__ import annotations

from .models import ComponentType, ExtensionRisk, ExtensionStrategy

# --- permission vocabulary --------------------------------------------------
#
# Flat strings, like Phase 3C's (spec §14) — no hierarchy, no roles.  These name
# what a *construction* phase would need; nothing here is ever granted.

CAPABILITY_ENABLE = "capability.enable"
PROCESS_CONFIGURE = "process.configure"
PROCESS_REGISTER = "process.register"
REPOSITORY_READ = "repository.read"
REPOSITORY_MODIFY = "repository.modify"
PROCESS_EXECUTE = "process.execute"
NETWORK_ACCESS = "network.access"
NETWORK_UNRESTRICTED = "network.unrestricted"
PLUGIN_INSTALL = "plugin.install"
RUNTIME_MODIFY = "runtime.modify"
PERMISSION_MODIFY = "permission.modify"
FILESYSTEM_WRITE = "filesystem.write"

#: Permissions that make a proposal at least HIGH risk (spec §78).  Each one
#: reaches outside the extension being described: into the repository, into
#: other processes, or onto the network.
ESCALATING_PERMISSIONS: frozenset[str] = frozenset(
    {
        REPOSITORY_MODIFY,
        PROCESS_EXECUTE,
        NETWORK_UNRESTRICTED,
        PLUGIN_INSTALL,
        "shell.execute",
    }
)

#: Permissions that would let an extension change the rules it is judged by.
#: Always CRITICAL: a proposal that can modify the runtime or the permission
#: model is not a proposal about a capability any more (spec §79, Invariant 84).
CRITICAL_PERMISSIONS: frozenset[str] = frozenset({RUNTIME_MODIFY, PERMISSION_MODIFY})

#: Metadata flags a component may carry that put it beyond Phase 5A's boundary.
MODIFIES_CORE_FLAG = "modifies_core"
MODIFIES_POLICY_FLAG = "modifies_policy"


# --- the three tables -------------------------------------------------------

#: Reuse first, construction last (spec §17).  The index in this tuple is a
#: candidate's rank, so "prefer reuse" is arithmetic rather than advice.
STRATEGY_ORDER: tuple[ExtensionStrategy, ...] = (
    ExtensionStrategy.REGISTER_EXISTING_PROCESS,
    ExtensionStrategy.CONFIGURE_EXISTING_PROCESS,
    ExtensionStrategy.CONNECT_EXISTING_BACKEND,
    ExtensionStrategy.ADD_ADAPTER,
    ExtensionStrategy.ADD_EXTRACTOR,
    ExtensionStrategy.ADD_PROCESS_DEFINITION,
    ExtensionStrategy.ADD_EXTERNAL_PLUGIN,
    ExtensionStrategy.CODE_EXTENSION,
    ExtensionStrategy.UNSUPPORTED,
)

#: What each strategy touches, before permissions are considered (spec §40).
STRATEGY_RISK: dict[ExtensionStrategy, ExtensionRisk] = {
    ExtensionStrategy.REGISTER_EXISTING_PROCESS: ExtensionRisk.LOW,
    ExtensionStrategy.CONFIGURE_EXISTING_PROCESS: ExtensionRisk.LOW,
    ExtensionStrategy.CONNECT_EXISTING_BACKEND: ExtensionRisk.LOW,
    ExtensionStrategy.ADD_ADAPTER: ExtensionRisk.MEDIUM,
    ExtensionStrategy.ADD_EXTRACTOR: ExtensionRisk.MEDIUM,
    ExtensionStrategy.ADD_PROCESS_DEFINITION: ExtensionRisk.MEDIUM,
    ExtensionStrategy.ADD_EXTERNAL_PLUGIN: ExtensionRisk.HIGH,
    ExtensionStrategy.CODE_EXTENSION: ExtensionRisk.HIGH,
    ExtensionStrategy.UNSUPPORTED: ExtensionRisk.CRITICAL,
}

#: What building each strategy would need permission to do (spec §76).
STRATEGY_PERMISSIONS: dict[ExtensionStrategy, tuple[str, ...]] = {
    ExtensionStrategy.REGISTER_EXISTING_PROCESS: (CAPABILITY_ENABLE,),
    ExtensionStrategy.CONFIGURE_EXISTING_PROCESS: (PROCESS_CONFIGURE,),
    ExtensionStrategy.CONNECT_EXISTING_BACKEND: (PROCESS_CONFIGURE,),
    ExtensionStrategy.ADD_ADAPTER: (REPOSITORY_READ, PROCESS_REGISTER),
    ExtensionStrategy.ADD_EXTRACTOR: (REPOSITORY_READ, PROCESS_REGISTER),
    ExtensionStrategy.ADD_PROCESS_DEFINITION: (REPOSITORY_READ, PROCESS_REGISTER),
    ExtensionStrategy.ADD_EXTERNAL_PLUGIN: (PLUGIN_INSTALL, NETWORK_ACCESS),
    ExtensionStrategy.CODE_EXTENSION: (REPOSITORY_MODIFY, FILESYSTEM_WRITE),
    ExtensionStrategy.UNSUPPORTED: (),
}

#: The component each strategy is expected to produce.  Reuse strategies
#: produce none — that is what makes them reuse.
STRATEGY_COMPONENT_TYPE: dict[ExtensionStrategy, ComponentType | None] = {
    ExtensionStrategy.REGISTER_EXISTING_PROCESS: None,
    ExtensionStrategy.CONFIGURE_EXISTING_PROCESS: ComponentType.CONFIGURATION,
    ExtensionStrategy.CONNECT_EXISTING_BACKEND: ComponentType.CONFIGURATION,
    ExtensionStrategy.ADD_ADAPTER: ComponentType.INGRESS_ADAPTER,
    ExtensionStrategy.ADD_EXTRACTOR: ComponentType.RESOURCE_EXTRACTOR,
    ExtensionStrategy.ADD_PROCESS_DEFINITION: ComponentType.PROCESS_DEFINITION,
    ExtensionStrategy.ADD_EXTERNAL_PLUGIN: ComponentType.PLUGIN,
    ExtensionStrategy.CODE_EXTENSION: ComponentType.CODE_MODULE,
    ExtensionStrategy.UNSUPPORTED: None,
}

#: Strategies that would put new code into this repository.  Kept as a set
#: rather than inferred from the risk table: "writes code" and "is risky" are
#: different questions that happen to agree today.
NEW_CODE_STRATEGIES: frozenset[ExtensionStrategy] = frozenset(
    {
        ExtensionStrategy.ADD_ADAPTER,
        ExtensionStrategy.ADD_EXTRACTOR,
        ExtensionStrategy.ADD_PROCESS_DEFINITION,
        ExtensionStrategy.CODE_EXTENSION,
    }
)

#: Strategies that reuse something the system already has (Invariant 88).
REUSE_STRATEGIES: frozenset[ExtensionStrategy] = frozenset(
    {
        ExtensionStrategy.REGISTER_EXISTING_PROCESS,
        ExtensionStrategy.CONFIGURE_EXISTING_PROCESS,
        ExtensionStrategy.CONNECT_EXISTING_BACKEND,
    }
)

#: A rough relative build cost, used only to order candidates that are
#: otherwise tied.  Not money and not hours — a preference, made explicit.
STRATEGY_COST: dict[ExtensionStrategy, float] = {
    ExtensionStrategy.REGISTER_EXISTING_PROCESS: 0.0,
    ExtensionStrategy.CONFIGURE_EXISTING_PROCESS: 1.0,
    ExtensionStrategy.CONNECT_EXISTING_BACKEND: 2.0,
    ExtensionStrategy.ADD_ADAPTER: 5.0,
    ExtensionStrategy.ADD_EXTRACTOR: 5.0,
    ExtensionStrategy.ADD_PROCESS_DEFINITION: 6.0,
    ExtensionStrategy.ADD_EXTERNAL_PLUGIN: 8.0,
    ExtensionStrategy.CODE_EXTENSION: 13.0,
    ExtensionStrategy.UNSUPPORTED: 0.0,
}


def strategy_rank(strategy: ExtensionStrategy | None) -> int:
    """Where ``strategy`` sits in the reuse-first order; unknown sorts last."""
    if strategy is None:
        return len(STRATEGY_ORDER)
    try:
        return STRATEGY_ORDER.index(strategy)
    except ValueError:  # pragma: no cover - every member is in the tuple
        return len(STRATEGY_ORDER)


def implied_permissions(strategy: ExtensionStrategy | None, components=()) -> list[str]:
    """Every permission this extension would need, strategy and components.

    The union, deduplicated and stable in order.  A proposal declaring less
    than this is under-declaring, which validation refuses (spec §17 applied to
    extensions) — otherwise a proposal could lower its own risk simply by
    saying less about itself.
    """
    permissions: list[str] = list(STRATEGY_PERMISSIONS.get(strategy, ()))
    for component in components or ():
        permissions.extend(getattr(component, "required_permissions", ()) or ())
    return list(dict.fromkeys(permissions))


def has_unknown_component(components=()) -> bool:
    """Whether any component names a type this system does not understand."""
    return any(
        not ComponentType.known(getattr(c, "component_type", "")) for c in components or ()
    )


def touches_core(components=()) -> bool:
    """Whether any component declares that it would change the core or policy.

    Phase 5A's answer to *"add a seventh primitive"* (spec §38) and to
    *"relax the policy so this can pass"* (spec §79) is the same: recognise the
    request, classify it CRITICAL, and let policy refuse it.  Neither is a
    capability question.
    """
    for component in components or ():
        metadata = getattr(component, "metadata", None) or {}
        if metadata.get(MODIFIES_CORE_FLAG) or metadata.get(MODIFIES_POLICY_FLAG):
            return True
    return False


def classify_risk(
    strategy: ExtensionStrategy | None,
    *,
    components=(),
    permissions=(),
) -> ExtensionRisk:
    """Classify an extension's risk deterministically (spec §40–§41).

    The rule is that every input can only make the answer worse.  An unknown
    strategy, an unknown component type, a permission that reaches outside the
    extension, or a declared core/policy change each raise the floor; nothing
    lowers it.
    """
    if strategy is None or strategy is ExtensionStrategy.UNSUPPORTED:
        return ExtensionRisk.CRITICAL

    risk = STRATEGY_RISK.get(strategy, ExtensionRisk.CRITICAL)
    if has_unknown_component(components) or touches_core(components):
        return ExtensionRisk.CRITICAL

    declared = set(permissions or ()) | set(implied_permissions(strategy, components))
    if declared & CRITICAL_PERMISSIONS:
        return ExtensionRisk.CRITICAL
    if declared & ESCALATING_PERMISSIONS:
        risk = ExtensionRisk.highest(risk, ExtensionRisk.HIGH)
    return risk


__all__ = [
    "CAPABILITY_ENABLE",
    "CRITICAL_PERMISSIONS",
    "ESCALATING_PERMISSIONS",
    "FILESYSTEM_WRITE",
    "MODIFIES_CORE_FLAG",
    "MODIFIES_POLICY_FLAG",
    "NETWORK_ACCESS",
    "NETWORK_UNRESTRICTED",
    "NEW_CODE_STRATEGIES",
    "PERMISSION_MODIFY",
    "PLUGIN_INSTALL",
    "PROCESS_CONFIGURE",
    "PROCESS_EXECUTE",
    "PROCESS_REGISTER",
    "REPOSITORY_MODIFY",
    "REPOSITORY_READ",
    "REUSE_STRATEGIES",
    "RUNTIME_MODIFY",
    "STRATEGY_COMPONENT_TYPE",
    "STRATEGY_COST",
    "STRATEGY_ORDER",
    "STRATEGY_PERMISSIONS",
    "STRATEGY_RISK",
    "classify_risk",
    "has_unknown_component",
    "implied_permissions",
    "strategy_rank",
    "touches_core",
]
