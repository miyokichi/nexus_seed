"""The deliberately small human approval boundary for MVP."""

from __future__ import annotations

from collections.abc import Callable

from .models import ProjectProposal


class CLIHumanApproval:
    """Print a proposal and ask the terminal operator to approve it."""

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self.input_fn = input_fn
        self.output_fn = output_fn

    def approve(self, proposal: ProjectProposal) -> bool:
        """Approve only an explicit ``y`` or ``yes`` answer."""

        self.output_fn("Project Proposal")
        self.output_fn(f"Title: {proposal.title}")
        self.output_fn(f"Goal: {proposal.goal}")
        self.output_fn(f"Reason: {proposal.reason}")
        answer = self.input_fn("Approve? [y/N] ").strip().casefold()
        return answer in {"y", "yes"}


class FixedApproval:
    """Non-interactive approval policy for tests and explicit CLI ``--yes``."""

    def __init__(self, approved: bool) -> None:
        self.approved = approved

    def approve(self, proposal: ProjectProposal) -> bool:
        """Return the configured decision."""

        return self.approved


__all__ = ["CLIHumanApproval", "FixedApproval"]
