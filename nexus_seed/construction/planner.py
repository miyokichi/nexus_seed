"""Deterministic templates for turning WHAT/WHY into construction HOW."""

from __future__ import annotations

from ..extension.models import ExtensionStrategy
from .models import (
    ArtifactRole,
    CapabilityAssertion,
    CapabilityContract,
    ConstructionPlan,
    ConstructionStep,
    ConstructionStepType,
    ExpectedArtifact,
)


class ConstructionPlanner:
    """Build a bounded, deterministic plan for an approved proposal."""

    def build(
        self, proposal, gap, *, context_snapshot_id=None, attempt: int = 1
    ) -> ConstructionPlan:
        """Build one immutable logical attempt of a bounded construction."""
        targets = sorted(set(proposal.target_names or gap.missing_names))
        stem = self._safe_stem(targets[0] if targets else "extension")
        if proposal.strategy is ExtensionStrategy.ADD_EXTRACTOR:
            artifacts = [
                ExpectedArtifact(f"extractors/{stem}.py", ArtifactRole.IMPLEMENTATION.value),
                ExpectedArtifact(f"tests/test_{stem}.py", ArtifactRole.TEST.value),
                ExpectedArtifact(f"fixtures/{stem}.txt", ArtifactRole.FIXTURE.value),
            ]
        elif proposal.strategy is ExtensionStrategy.ADD_PROCESS_DEFINITION:
            artifacts = [
                ExpectedArtifact(f"processes/{stem}.py", ArtifactRole.IMPLEMENTATION.value),
                ExpectedArtifact(f"tests/test_{stem}.py", ArtifactRole.TEST.value),
                ExpectedArtifact(f"metadata/{stem}.json", ArtifactRole.CONFIGURATION.value),
            ]
        elif proposal.strategy is ExtensionStrategy.REGISTER_EXISTING_PROCESS:
            artifacts = [
                ExpectedArtifact(f"manifests/{stem}.json", ArtifactRole.MANIFEST.value),
            ]
        else:
            artifacts = [
                ExpectedArtifact(f"extensions/{stem}.py", ArtifactRole.IMPLEMENTATION.value),
                ExpectedArtifact(f"tests/test_{stem}.py", ArtifactRole.TEST.value),
            ]

        contract = CapabilityContract(
            capability_name=targets[0] if targets else "",
            input_fixture={"kind": proposal.strategy.value if proposal.strategy else "UNKNOWN"},
            expected_output_type="dict",
            assertions=[
                {"type": CapabilityAssertion.NO_EXCEPTION.value},
                {"type": CapabilityAssertion.OUTPUT_EXISTS.value},
            ],
        )
        plan = ConstructionPlan(
            extension_proposal_id=proposal.id,
            capability_gap_id=gap.id,
            work_requirement_id=gap.work_requirement_id,
            target_capabilities=targets,
            expected_artifacts=artifacts,
            verification_requirements=[
                {"layer": "STRUCTURAL", "required": True},
                {"layer": "STATIC", "required": True},
                {"layer": "BEHAVIOR", "required": True, "contract": contract.to_dict()},
            ],
            sandbox_requirements={
                "network": "DENY", "max_files": 32,
                "max_file_bytes": 256_000, "max_total_bytes": 1_000_000,
                "timeout_seconds": 10.0,
            },
            # Literal ceiling required by the spec: a construction plan may
            # ask for no production/global permission the approved proposal
            # did not already declare.  The Grant later translates these into
            # the separate sandbox.* vocabulary.
            required_permissions=sorted(set(proposal.required_permissions)),
            context_snapshot_id=context_snapshot_id,
            attempt=attempt,
        )
        plan.steps = [
            ConstructionStep(
                plan_id=plan.id, step_index=0,
                step_type=ConstructionStepType.GENERATE_CODE,
                description="Generate the declared artifacts inside the sandbox",
                inputs={"strategy": proposal.strategy.value if proposal.strategy else None},
                expected_outputs=[a.to_dict() for a in artifacts],
                required_permissions=["sandbox.write"],
            ),
            ConstructionStep(
                plan_id=plan.id, step_index=1,
                step_type=ConstructionStepType.RUN_STATIC_CHECK,
                description="Validate structure, syntax and imports",
                inputs={"paths": [a.relative_path for a in artifacts]},
                required_permissions=["sandbox.read", "sandbox.test"],
            ),
            ConstructionStep(
                plan_id=plan.id, step_index=2,
                step_type=ConstructionStepType.RUN_TEST,
                description="Verify the target capability contract",
                inputs={"contract": contract.to_dict()},
                required_permissions=["sandbox.read", "sandbox.test"],
            ),
        ]
        return plan

    @staticmethod
    def _safe_stem(value: str) -> str:
        safe = "".join(c.lower() if c.isalnum() else "_" for c in value).strip("_")
        return safe or "extension"
