"""Small deterministic Project Planner for the first autonomous loop."""

from __future__ import annotations

from nexus_planner.contracts.mvp import JsonValue, KnowledgeItem, ProjectProposal


class SimpleProjectPlanner:
    """Propose one Project for each non-empty observation.

    The planner deliberately uses no capability planning, goal hierarchy or
    orchestration.  It can be replaced behind the same ``propose`` method when
    more judgement is needed.
    """

    def propose(self, context: KnowledgeItem) -> list[ProjectProposal]:
        """Return one minimal proposal when ``context`` contains actionable text."""

        content, source, context_id = _unpack(context)
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            return []

        if "readme" in text.casefold():
            title = "README改善"
            goal = "READMEを確認し、改善案を作成する"
        else:
            compact = " ".join(text.split())
            title = compact[:60] + ("…" if len(compact) > 60 else "")
            goal = compact
        return [
            ProjectProposal(
                title=title,
                goal=goal,
                reason=f"{source} から実行可能な依頼を観測したため",
                context={
                    "observation_id": context_id,
                    "observation": content,
                    "source": source,
                },
            )
        ]


def _unpack(context: KnowledgeItem) -> tuple[JsonValue, str, str]:
    return context.content, context.source, context.id


__all__ = ["SimpleProjectPlanner"]
