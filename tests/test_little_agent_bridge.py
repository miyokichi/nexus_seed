"""The little_agent boundary translates A2A metadata and nothing semantic."""

from nexus_seed.integrations.agent_runtime.little_agent_bridge import (
    LITTLE_AGENT_ALLOWED_PATHS_KEY,
    LITTLE_AGENT_WORKSPACE_KEY,
    little_agent_message_metadata,
)


def test_translates_workspace_and_deduplicates_readonly_paths():
    metadata = little_agent_message_metadata(
        workspace="/work/project-1",
        readable_paths=["/source/input.txt", "/source/input.txt", ""],
    )

    assert metadata == {
        LITTLE_AGENT_WORKSPACE_KEY: "/work/project-1",
        LITTLE_AGENT_ALLOWED_PATHS_KEY: ["/source/input.txt"],
    }


def test_omits_absent_grants_instead_of_inventing_paths():
    assert little_agent_message_metadata(workspace=None, readable_paths=[]) == {}
