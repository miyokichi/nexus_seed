"""Work grants: handing a peer a workspace, and refusing the ones you may not.

A grant is a request, never an authorization, so the interesting cases are the
refusals. Both checks are covered: the caller may only hand out what it can
write, and the server authorizes again on arrival.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from little_agent.config import builtin_skills_dir
from little_agent.a2a.grant import (
    ALLOWED_PATHS_METADATA_KEY,
    WORKSPACE_METADATA_KEY,
    GrantError,
    GrantPolicy,
    WorkGrant,
)
from little_agent.config import AgentConfig
from little_agent.tools.base import PathAccessPolicy

LIBRARY = builtin_skills_dir()


def _config(workspace: Path, writable: tuple[Path, ...] = (), readable: tuple[Path, ...] = ()):
    return AgentConfig(
        model="local",
        workspace=workspace.resolve(),
        require_confirmation=False,
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        enable_logging=False,
        skill_library_dir=LIBRARY,
        agents_dir=(workspace / "agents").resolve(),
        readable_paths=tuple(path.resolve() for path in readable),
        writable_paths=tuple(path.resolve() for path in writable),
    )


class WireFormatTests(unittest.TestCase):
    def test_round_trips_through_message_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            grant = WorkGrant.request(
                root, workspace="reports/q3", allowed_paths=[str(root / "sales.xlsx")]
            )
            metadata = grant.to_metadata()
            self.assertEqual(metadata[WORKSPACE_METADATA_KEY], str(root / "reports" / "q3"))
            self.assertEqual(metadata[ALLOWED_PATHS_METADATA_KEY], [str(root / "sales.xlsx")])
            self.assertEqual(WorkGrant.from_metadata(metadata), grant)

    def test_empty_grant_carries_no_metadata(self) -> None:
        self.assertTrue(WorkGrant().is_empty)
        self.assertEqual(WorkGrant().to_metadata(), {})

    def test_unrelated_metadata_is_ignored(self) -> None:
        self.assertTrue(WorkGrant.from_metadata({"littleAgent/delegationDepth": 1}).is_empty)
        self.assertTrue(WorkGrant.from_metadata("not a dict").is_empty)

    def test_relative_entries_resolve_against_the_base(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            grant = WorkGrant.request(root, workspace="sub", allowed_paths=["notes.md"])
            self.assertEqual(grant.workspace, root / "sub")
            self.assertEqual(grant.allowed_paths, (root / "notes.md",))

    def test_a_single_string_is_accepted_as_one_allowed_path(self) -> None:
        with TemporaryDirectory() as tmp:
            grant = WorkGrant.request(Path(tmp), allowed_paths="notes.md")
            self.assertEqual(len(grant.allowed_paths), 1)

    def test_a_non_list_allowed_paths_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(GrantError):
                WorkGrant.request(Path(tmp), allowed_paths={"bad": True})


class ApplyTests(unittest.TestCase):
    def test_workspace_is_the_writable_home_and_allowed_paths_are_read_only(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = _config(root)
            granted = WorkGrant(
                workspace=root / "case-7", allowed_paths=(root / "shared",)
            ).apply(config)
            self.assertEqual(granted.workspace, root / "case-7")
            self.assertEqual(granted.readable_paths, (root / "shared",))
            self.assertNotIn(root / "shared", granted.writable_paths)

    def test_the_resulting_policy_lets_the_task_read_but_not_write_an_allowed_path(self) -> None:
        """The grant has to survive contact with the path policy the tools use."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            reference = root / "shared" / "prices.xlsx"
            reference.parent.mkdir(parents=True)
            reference.write_text("x", encoding="utf-8")
            granted = WorkGrant(
                workspace=root / "case-7", allowed_paths=(root / "shared",)
            ).apply(_config(root))
            policy = PathAccessPolicy(
                granted.workspace, granted.readable_paths, granted.writable_paths
            )

            self.assertEqual(policy.resolve(str(reference), access="read"), reference)
            with self.assertRaises(ValueError):
                policy.resolve(str(reference), access="write")

    def test_an_empty_grant_changes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            self.assertIs(WorkGrant().apply(config), config)

    def test_omitted_paths_keep_the_server_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = _config(root, writable=(root / "own",), readable=(root / "ref",))
            granted = WorkGrant(workspace=root / "case-7").apply(config)
            self.assertEqual(granted.readable_paths, (root / "ref",))

    def test_a_grant_never_touches_the_servers_own_writable_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = _config(root, writable=(root / "own",))
            granted = WorkGrant(
                workspace=root / "case-7", allowed_paths=(root / "shared",)
            ).apply(config)
            self.assertEqual(granted.writable_paths, (root / "own",))


class PolicyTests(unittest.TestCase):
    def test_a_path_inside_the_workspace_is_allowed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            policy = GrantPolicy.from_config(_config(root))
            grant = WorkGrant(workspace=root / "case-7")
            self.assertEqual(policy.authorize(grant).workspace, root / "case-7")

    def test_a_path_outside_every_root_is_refused(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as other:
            policy = GrantPolicy.from_config(_config(Path(tmp)))
            with self.assertRaises(GrantError) as caught:
                policy.authorize(WorkGrant(workspace=Path(other).resolve()))
            self.assertIn("outside the writable paths", str(caught.exception))

    def test_a_configured_writable_path_may_be_granted(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as shared:
            shared_root = Path(shared).resolve()
            policy = GrantPolicy.from_config(_config(Path(tmp), writable=(shared_root,)))
            grant = WorkGrant(allowed_paths=(shared_root / "prices.xlsx",))
            self.assertEqual(policy.authorize(grant).allowed_paths, (shared_root / "prices.xlsx",))

    def test_writable_roots_are_readable_too(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            policy = GrantPolicy(writable_roots=(root,))
            self.assertIn(root, policy.readable_roots)

    def test_a_readable_path_may_be_granted_as_an_allowed_path(self) -> None:
        """allowed_paths convey read only, so read-only reach is enough to pass on."""

        with TemporaryDirectory() as tmp, TemporaryDirectory() as reference:
            reference_root = Path(reference).resolve()
            policy = GrantPolicy.from_config(_config(Path(tmp), readable=(reference_root,)))
            grant = WorkGrant(allowed_paths=(reference_root / "notes.md",))
            self.assertEqual(
                policy.authorize(grant).allowed_paths, (reference_root / "notes.md",)
            )

    def test_a_readable_path_may_not_become_a_workspace(self) -> None:
        """A workspace is written in, so read-only reach is not enough."""

        with TemporaryDirectory() as tmp, TemporaryDirectory() as reference:
            reference_root = Path(reference).resolve()
            policy = GrantPolicy.from_config(_config(Path(tmp), readable=(reference_root,)))
            with self.assertRaises(GrantError) as caught:
                policy.authorize(WorkGrant(workspace=reference_root))
            self.assertIn("outside the writable paths", str(caught.exception))

    def test_a_writable_path_may_be_granted_either_way(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as shared:
            shared_root = Path(shared).resolve()
            policy = GrantPolicy.from_config(_config(Path(tmp), writable=(shared_root,)))
            self.assertEqual(
                policy.authorize(WorkGrant(workspace=shared_root)).workspace, shared_root
            )
            self.assertEqual(
                policy.authorize(WorkGrant(allowed_paths=(shared_root,))).allowed_paths,
                (shared_root,),
            )

    def test_an_unreachable_path_is_refused_as_an_allowed_path_too(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as outside:
            policy = GrantPolicy.from_config(_config(Path(tmp)))
            with self.assertRaises(GrantError) as caught:
                policy.authorize(WorkGrant(allowed_paths=(Path(outside).resolve(),)))
            self.assertIn("outside the readable paths", str(caught.exception))

    def test_allow_any_accepts_anything(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as other:
            policy = GrantPolicy.from_config(_config(Path(tmp)), allow_any=True)
            grant = WorkGrant(workspace=Path(other).resolve())
            self.assertEqual(policy.authorize(grant).workspace, Path(other).resolve())

    def test_an_empty_grant_needs_no_authorization(self) -> None:
        policy = GrantPolicy(writable_roots=())
        self.assertTrue(policy.authorize(WorkGrant()).is_empty)

    def test_a_granted_file_root_only_matches_itself(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            granted_file = root / "prices.xlsx"
            granted_file.write_text("x", encoding="utf-8")
            sibling = root / "secret.xlsx"
            sibling.write_text("x", encoding="utf-8")

            policy = GrantPolicy(writable_roots=(granted_file,))
            self.assertEqual(
                policy.authorize(WorkGrant(allowed_paths=(granted_file,))).allowed_paths,
                (granted_file,),
            )
            with self.assertRaises(GrantError):
                policy.authorize(WorkGrant(allowed_paths=(sibling,)))


if __name__ == "__main__":
    unittest.main()
