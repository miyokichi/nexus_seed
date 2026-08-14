"""Deterministic Phase 5C installation-plan construction."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..extension.models import ExtensionStrategy
from .models import (
    ArtifactIdentity,
    InstallationPlan,
    InstallationStep,
    InstallationStepType,
)


def _safe_name(value: str) -> str:
    safe = "".join(c.lower() if c.isalnum() else "_" for c in value).strip("_")
    return safe or "extension"


class InstallationPlanner:
    """Bind a VERIFIED construction result to versioned production paths."""

    def build(
        self,
        result,
        construction_plan,
        proposal,
        *,
        resource_store,
        workspace,
        capability_registry=None,
        installation_store=None,
        context_snapshot_id=None,
    ) -> InstallationPlan:
        component = proposal.proposed_components[0] if proposal.proposed_components else None
        reusable = proposal.reusable_components[0] if proposal.reusable_components else ""
        reusable_name, reusable_sep, reusable_version = reusable.partition(":")
        component_name = _safe_name(
            getattr(component, "name", "")
            or reusable_name
            or (result.provided_capabilities[0] if result.provided_capabilities else "extension")
        )
        versions = []
        for resource_id in result.artifact_resource_ids:
            resource = resource_store.get_resource(resource_id)
            if resource is None or resource.current_version_id is None:
                continue
            version = resource_store.get_version(resource.current_version_id)
            if version is not None:
                versions.append((resource, version))
        digest = hashlib.sha256(
            "|".join(sorted(str(v.content_hash or "") for _, v in versions)).encode("utf-8")
        ).hexdigest()
        declared_version = str((getattr(component, "metadata", {}) or {}).get("version", ""))
        component_version = (
            _safe_name(declared_version)
            if declared_version
            else "1"
            if proposal.strategy is ExtensionStrategy.REGISTER_EXISTING_PROCESS
            else digest[:12]
        )
        artifact_versions: list[ArtifactIdentity] = []
        workspace_root = Path(workspace.root_locator).resolve()
        for resource, version in versions:
            locator = Path(version.locator).resolve()
            try:
                relative = locator.relative_to(workspace_root).as_posix()
            except ValueError:
                relative = Path(version.locator).name
            destination = f"{component_name}/{component_version}/{relative}"
            artifact_versions.append(ArtifactIdentity(
                resource_id=resource.id,
                resource_version_id=version.id,
                content_hash=str(version.content_hash or ""),
                locator=version.locator,
                relative_path=relative,
                artifact_role=str(resource.metadata.get("artifact_role", "")),
                destination=destination,
            ))

        strategy = proposal.strategy.value if proposal.strategy else str(proposal.declared_strategy or "")
        definition_name = (
            reusable_name
            if proposal.strategy is ExtensionStrategy.REGISTER_EXISTING_PROCESS and proposal.reusable_components
            else f"installed_{component_name}"
        )
        definition_version = str((getattr(component, "metadata", {}) or {}).get(
            "definition_version",
            (reusable_version or "1") if proposal.strategy is ExtensionStrategy.REGISTER_EXISTING_PROCESS else component_version,
        ))
        registry_changes = [{
            "strategy": strategy,
            "component_name": component_name,
            "component_version": component_version,
            "definition_name": definition_name,
            "definition_version": definition_version,
            "target_capabilities": list(result.provided_capabilities),
        }]
        previous_capabilities = {}
        if capability_registry is not None:
            for name in result.provided_capabilities:
                current = capability_registry.get_capability(name)
                if current is not None:
                    previous_capabilities[name] = {
                        "version": current.version,
                        "providers": [list(v) for v in capability_registry.get_processes_providing(
                            current.name, current.version
                        )],
                    }
        previous_activations = []
        if installation_store is not None:
            wanted = set(result.provided_capabilities)
            previous_activations = [
                str(record.id) for record in installation_store.active_activations()
                if wanted & set(record.capabilities)
            ]
        previous_state = {
            "active_capabilities": previous_capabilities,
            "active_activation_ids": previous_activations,
        }
        rollback_spec = {
            "previous_state": previous_state,
            "new_installed_root": f"{component_name}/{component_version}",
            "registry_changes": registry_changes,
            "strategy": "disable_new_and_restore_previous",
        }
        plan = InstallationPlan(
            construction_result_id=result.id,
            extension_proposal_id=result.extension_proposal_id,
            capability_gap_id=construction_plan.capability_gap_id,
            work_requirement_id=construction_plan.work_requirement_id,
            artifact_versions=artifact_versions,
            target_capabilities=list(result.provided_capabilities),
            production_destinations=[a.destination for a in artifact_versions],
            registry_changes=registry_changes,
            required_permissions=list(proposal.required_permissions),
            rollback_spec=rollback_spec,
            component_name=component_name,
            component_version=component_version,
            strategy=strategy,
            context_snapshot_id=context_snapshot_id,
        )
        index = 0
        for artifact in artifact_versions:
            plan.steps.append(InstallationStep(
                installation_plan_id=plan.id,
                step_index=index,
                step_type=InstallationStepType.COPY_VERIFIED_ARTIFACT,
                description=f"Copy verified {artifact.relative_path}",
                inputs=artifact.to_dict(),
                required_permissions=["production.install"],
            ))
            index += 1
        register_type = {
            ExtensionStrategy.ADD_EXTRACTOR.value: InstallationStepType.REGISTER_EXTRACTOR,
            ExtensionStrategy.ADD_PROCESS_DEFINITION.value: InstallationStepType.REGISTER_PROCESS_DEFINITION,
            ExtensionStrategy.REGISTER_EXISTING_PROCESS.value: InstallationStepType.ENABLE_COMPONENT,
        }.get(strategy, InstallationStepType.REGISTER_COMPONENT)
        plan.steps.extend([
            InstallationStep(
                installation_plan_id=plan.id, step_index=index,
                step_type=InstallationStepType.RUN_SMOKE_TEST,
                description="Load production copy and run a side-effect-free smoke fixture",
                required_permissions=["production.read"],
            ),
            InstallationStep(
                installation_plan_id=plan.id, step_index=index + 1,
                step_type=register_type,
                description="Apply the exact reviewed registry mutation",
                inputs=registry_changes[0],
                required_permissions=["capability.enable"],
            ),
            InstallationStep(
                installation_plan_id=plan.id, step_index=index + 2,
                step_type=InstallationStepType.SET_ACTIVE_VERSION,
                description="Switch active registry state only after smoke verification",
                inputs={"component": component_name, "version": component_version},
                required_permissions=["capability.enable"],
            ),
        ])
        return plan


__all__ = ["InstallationPlanner"]
