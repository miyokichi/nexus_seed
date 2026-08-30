"""Task workspaces: what is granted, what is refused, what comes back.

The boundary these tests pin is not "the Agent behaves". It is that NEXUS SEED
decides, and that a task cannot reach past what it was given even if the Agent
ignores every instruction it was sent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_seed.knowledge.ledger import KnowledgeLedger
from nexus_seed.resources.scope import ResourceScope
from nexus_seed.storage import Database, KnowledgeStore
from nexus_seed.workspace import (
    AccessMode,
    Delivery,
    GrantPolicy,
    GrantRequest,
    ResourceGrant,
    WorkspaceProvisioner,
)
from nexus_seed.workspace.provisioner import MANIFEST_PATH, README_PATH



def shared_tree(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "spec.md").write_text("共通仕様", encoding="utf-8")
    (shared / "plan.md").write_text("計画v1", encoding="utf-8")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "keys.txt").write_text("TOP SECRET", encoding="utf-8")
    return shared, outside


def policy_for(shared, *, writable=True, **kwargs):
    return GrantPolicy(
        scope=ResourceScope(
            read_roots=[shared], write_roots=[shared] if writable else []
        ),
        **kwargs,
    )


def request_for(uri, access=AccessMode.READ):
    return GrantRequest(project_id="project-1", requested="x", uri=uri, access=access)


# --- policy: the host is not the workspace -----------------------------------


def test_a_path_outside_the_authorized_root_is_refused(tmp_path):
    shared, outside = shared_tree(tmp_path)
    policy = policy_for(shared)

    for uri in (
        "file:../private/keys.txt",
        f"file:{outside / 'keys.txt'}",
        "file:/etc/passwd",
    ):
        decision = policy.decide(request_for(uri))
        assert decision.allowed is False, uri
        assert decision.grant is None


def test_with_no_authorized_root_no_host_file_is_grantable(tmp_path):
    shared, _ = shared_tree(tmp_path)
    decision = GrantPolicy().decide(request_for("file:spec.md"))
    assert decision.allowed is False
    assert "no authorized file root" in decision.reason


def test_a_symlink_pointing_out_of_the_root_is_refused(tmp_path):
    shared, outside = shared_tree(tmp_path)
    try:
        (shared / "sneaky.txt").symlink_to(outside / "keys.txt")
    except OSError:
        pytest.skip("symlinks are not available in this environment")

    decision = policy_for(shared).decide(request_for("file:sneaky.txt"))

    assert decision.allowed is False
    assert "escapes" in decision.reason


def test_write_is_granted_separately_from_read(tmp_path):
    shared, _ = shared_tree(tmp_path)
    read_only_policy = policy_for(shared, writable=False)

    assert read_only_policy.decide(request_for("file:spec.md")).allowed is True
    refused = read_only_policy.decide(request_for("file:spec.md", AccessMode.READ_WRITE))
    assert refused.allowed is False


def test_a_request_naming_nothing_grantable_is_left_to_a_person(tmp_path):
    shared, _ = shared_tree(tmp_path)
    decision = policy_for(shared).decide(
        GrantRequest(project_id="p", requested="last year's SAP export")
    )
    assert decision.allowed is False
    assert "a person has to say" in decision.reason


def test_an_unknown_scheme_is_refused(tmp_path):
    shared, _ = shared_tree(tmp_path)
    assert policy_for(shared).decide(request_for("ssh:somewhere")).allowed is False


def test_a_missing_file_is_refused_rather_than_granted_empty(tmp_path):
    shared, _ = shared_tree(tmp_path)
    decision = policy_for(shared).decide(request_for("file:absent.md"))
    assert decision.allowed is False
    assert "does not exist" in decision.reason


# --- provisioning -------------------------------------------------------------


def test_granted_resources_appear_in_the_workspace(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"

    manifest = provisioner.provision(
        workspace,
        [
            ResourceGrant(uri="file:spec.md", reason="参照用"),
            ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE),
        ],
    )

    # Read is a link to the original; write is a copy in the workspace.
    assert [entry["path"] for entry in manifest.entries] == [
        str(shared / "spec.md"),
        "resources/plan.md",
    ]
    assert not (workspace / "resources/spec.md").exists()
    assert (workspace / "resources/plan.md").read_text(encoding="utf-8") == "計画v1"
    assert manifest.readable_paths == [str(shared / "spec.md")]
    assert manifest.writable_paths == [str(workspace / "resources/plan.md")]


def test_a_refused_grant_is_simply_not_provisioned(tmp_path):
    shared, outside = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))

    manifest = provisioner.provision(
        tmp_path / "ws",
        [
            ResourceGrant(uri="file:spec.md"),
            ResourceGrant(uri=f"file:{outside / 'keys.txt'}"),
        ],
    )

    assert [entry["uri"] for entry in manifest.entries] == ["file:spec.md"]
    assert not (tmp_path / "ws/resources/keys.txt").exists()


def test_a_grant_is_rechecked_when_the_root_narrows(tmp_path):
    """A stored grant must not outlive the permission it was made under."""
    shared, _ = shared_tree(tmp_path)
    grant = ResourceGrant(uri="file:spec.md", access=AccessMode.READ_WRITE)
    narrowed = WorkspaceProvisioner(policy_for(shared, writable=False))

    manifest = narrowed.provision(tmp_path / "ws", [grant])

    assert manifest.entries == []


def test_two_grants_with_the_same_name_do_not_collide(tmp_path):
    shared, _ = shared_tree(tmp_path)
    nested = shared / "sub"
    nested.mkdir()
    (nested / "spec.md").write_text("別の仕様", encoding="utf-8")
    provisioner = WorkspaceProvisioner(policy_for(shared))

    manifest = provisioner.provision(
        tmp_path / "ws",
        [
            ResourceGrant(uri="file:spec.md", access=AccessMode.READ_WRITE),
            ResourceGrant(uri="file:sub/spec.md", access=AccessMode.READ_WRITE),
        ],
    )

    paths = [entry["path"] for entry in manifest.entries]
    assert paths == ["resources/spec.md", "resources/spec-1.md"]
    assert (tmp_path / "ws/resources/spec-1.md").read_text(encoding="utf-8") == "別の仕様"


def test_an_explicit_name_is_what_the_agent_sees(tmp_path):
    shared, _ = shared_tree(tmp_path)
    manifest = WorkspaceProvisioner(policy_for(shared)).provision(
        tmp_path / "ws",
        [ResourceGrant(uri="file:spec.md", name="仕様.md",
                       access=AccessMode.READ_WRITE)],
    )
    assert manifest.entries[0]["path"] == "resources/仕様.md"


def test_the_workspace_says_what_is_in_it_two_ways(tmp_path):
    shared, _ = shared_tree(tmp_path)
    workspace = tmp_path / "ws"
    WorkspaceProvisioner(policy_for(shared)).provision(
        workspace,
        [
            ResourceGrant(uri="file:spec.md", reason="参照用"),
            ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE),
        ],
    )

    manifest = json.loads((workspace / MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest["readable_paths"] == [str(shared / "spec.md")]
    assert manifest["writable_paths"] == [str(workspace / "resources/plan.md")]

    # An Agent that reads files but not A2A metadata still learns what it has.
    readme = (workspace / README_PATH).read_text(encoding="utf-8")
    assert str(shared / "spec.md") in readme
    assert "resources/plan.md" in readme
    assert "NEED_RESOURCE" in readme


def test_a_writable_grant_never_names_the_host_path(tmp_path):
    """For a copy the Agent is told what it has, not where it came from.

    A read grant is the opposite by design — the path *is* the grant — so
    this holds only for what was copied.
    """
    shared, _ = shared_tree(tmp_path)
    workspace = tmp_path / "ws"
    WorkspaceProvisioner(policy_for(shared)).provision(
        workspace, [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE)]
    )

    entry = json.loads((workspace / MANIFEST_PATH).read_text(encoding="utf-8"))
    assert entry["resources"][0]["path"] == "resources/plan.md"
    assert str(shared / "plan.md") not in json.dumps(entry["resources"])


# --- what a read grant and a write grant each guarantee -------------------------


def test_a_read_grant_is_never_copied_and_never_collected(tmp_path):
    """A read is a link: nothing is duplicated, and nothing comes back."""
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    grants = [ResourceGrant(uri="file:spec.md")]

    manifest = provisioner.provision(workspace, grants)

    assert manifest.entries[0]["path"] == str(shared / "spec.md")
    assert list((workspace / "resources").iterdir()) == []
    assert provisioner.collect(workspace, grants) == []


def test_a_read_write_grant_is_carried_back_when_work_is_accepted(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    grants = [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE)]
    provisioner.provision(workspace, grants)

    (workspace / "resources/plan.md").write_text("計画v2", encoding="utf-8")
    written = provisioner.collect(workspace, grants)

    assert written == ["file:plan.md"]
    assert (shared / "plan.md").read_text(encoding="utf-8") == "計画v2"


def test_nothing_is_carried_back_before_it_is_collected(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    provisioner.provision(
        workspace, [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE)]
    )

    (workspace / "resources/plan.md").write_text("途中の状態", encoding="utf-8")

    assert (shared / "plan.md").read_text(encoding="utf-8") == "計画v1"


# --- Knowledge as a grantable resource ------------------------------------------


def test_knowledge_can_be_provided_as_a_file(tmp_path):
    ledger = KnowledgeLedger(KnowledgeStore(Database(tmp_path / "k.db")))
    item = ledger.record("契約Aは9月末で期限", source_type="meeting")
    shared, _ = shared_tree(tmp_path)
    policy = policy_for(shared, ledger=ledger)
    workspace = tmp_path / "ws"

    manifest = WorkspaceProvisioner(policy, ledger=ledger).provision(
        workspace, [ResourceGrant(uri=f"knowledge:{item.knowledge_id}")]
    )

    [entry] = manifest.entries
    assert (workspace / entry["path"]).read_text(encoding="utf-8") == "契約Aは9月末で期限"


def test_knowledge_is_never_granted_writable(tmp_path):
    ledger = KnowledgeLedger(KnowledgeStore(Database(tmp_path / "k.db")))
    item = ledger.record("記録", source_type="meeting")
    shared, _ = shared_tree(tmp_path)
    policy = policy_for(shared, ledger=ledger)

    decision = policy.decide(
        request_for(f"knowledge:{item.knowledge_id}", AccessMode.READ_WRITE)
    )

    assert decision.allowed is False
    assert "append-only" in decision.reason


def test_unknown_knowledge_is_refused(tmp_path):
    ledger = KnowledgeLedger(KnowledgeStore(Database(tmp_path / "k.db")))
    shared, _ = shared_tree(tmp_path)
    decision = policy_for(shared, ledger=ledger).decide(request_for("knowledge:K-nope"))
    assert decision.allowed is False


def test_knowledge_without_a_ledger_is_refused(tmp_path):
    shared, _ = shared_tree(tmp_path)
    decision = policy_for(shared).decide(request_for("knowledge:K-1"))
    assert decision.allowed is False
    assert "not available" in decision.reason


# --- re-provisioning, which is how a task continues -----------------------------


def test_re_provisioning_adds_the_new_grant_and_keeps_the_agent_s_own_work(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    provisioner.provision(workspace, [ResourceGrant(uri="file:spec.md")])
    (workspace / "notes.md").write_text("Agentの作業メモ", encoding="utf-8")

    manifest = provisioner.provision(
        workspace,
        [ResourceGrant(uri="file:spec.md"), ResourceGrant(uri="file:plan.md")],
    )

    assert [entry["path"] for entry in manifest.entries] == [
        str(shared / "spec.md"),
        str(shared / "plan.md"),
    ]
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "Agentの作業メモ"


def test_an_empty_grant_list_still_produces_a_usable_workspace(tmp_path):
    shared, _ = shared_tree(tmp_path)
    workspace = tmp_path / "ws"

    manifest = WorkspaceProvisioner(policy_for(shared)).provision(workspace, [])

    assert manifest.entries == []
    assert workspace.is_dir()
    assert "No resources beyond this workspace" in (
        workspace / README_PATH
    ).read_text(encoding="utf-8")


# --- read links, write copies ------------------------------------------------


def test_a_read_grant_hands_over_the_original_path(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"

    manifest = provisioner.provision(workspace, [ResourceGrant(uri="file:spec.md")])

    assert manifest.entries[0]["path"] == str(shared / "spec.md")
    assert manifest.entries[0]["delivery"] == "reference"
    assert manifest.readable_paths == [str(shared / "spec.md")]
    assert list((workspace / "resources").iterdir()) == []


def test_a_write_grant_reaches_the_original_only_when_collected(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    grants = [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE)]

    manifest = provisioner.provision(workspace, grants)
    Path(manifest.writable_paths[0]).write_text("計画v2", encoding="utf-8")

    # Still untouched while the work is in progress.
    assert (shared / "plan.md").read_text(encoding="utf-8") == "計画v1"

    assert provisioner.collect(workspace, grants) == ["file:plan.md"]
    assert (shared / "plan.md").read_text(encoding="utf-8") == "計画v2"


def test_an_abandoned_task_leaves_the_original_alone(tmp_path):
    """Nobody accepted the work, so nothing of it reached the real file."""
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    grants = [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE)]
    provisioner.provision(workspace, grants)

    (workspace / "resources/plan.md").write_text("途中で壊れた内容", encoding="utf-8")

    assert (shared / "plan.md").read_text(encoding="utf-8") == "計画v1"


def test_a_directory_can_be_read_but_not_written(tmp_path):
    shared, _ = shared_tree(tmp_path)
    (shared / "sub").mkdir(exist_ok=True)
    policy = policy_for(shared)

    allowed = policy.decide(GrantRequest(project_id="p", requested="sub", uri="file:sub"))
    refused = policy.decide(
        GrantRequest(
            project_id="p",
            requested="sub",
            uri="file:sub",
            access=AccessMode.READ_WRITE,
        )
    )

    assert allowed.allowed is True
    assert refused.allowed is False
    assert "directory" in refused.reason
    manifest = WorkspaceProvisioner(policy).provision(
        tmp_path / "ws", [ResourceGrant(uri="file:sub")]
    )
    assert manifest.readable_paths == [str(shared / "sub")]


def test_a_read_path_still_cannot_escape_the_authorized_root(tmp_path):
    shared, outside = shared_tree(tmp_path)
    try:
        (shared / "sneaky.txt").symlink_to(outside / "keys.txt")
    except OSError:
        pytest.skip("symlinks are not available in this environment")
    provisioner = WorkspaceProvisioner(policy_for(shared))

    manifest = provisioner.provision(
        tmp_path / "ws",
        [
            ResourceGrant(uri=f"file:{outside / 'keys.txt'}"),
            ResourceGrant(uri="file:sneaky.txt"),
            ResourceGrant(uri="file:spec.md"),
        ],
    )

    assert manifest.readable_paths == [str(shared / "spec.md")]


def test_a_record_has_no_path_so_reading_one_writes_it_out(tmp_path):
    """`knowledge:` and `resource:` are rows, not files: there is nothing to link."""
    shared, _ = shared_tree(tmp_path)
    db = Database(str(tmp_path / "k.db"))
    try:
        ledger = KnowledgeLedger(KnowledgeStore(db))
        item = ledger.record("決まったこと", source_type="meeting")
        provisioner = WorkspaceProvisioner(policy_for(shared, ledger=ledger))
        workspace = tmp_path / "ws"

        manifest = provisioner.provision(
            workspace, [ResourceGrant(uri=f"knowledge:{item.knowledge_id}")]
        )

        assert manifest.entries[0]["path"].startswith("resources/")
        assert manifest.readable_paths == [
            str(workspace / manifest.entries[0]["path"])
        ]
        assert "決まったこと" in (
            workspace / manifest.entries[0]["path"]
        ).read_text(encoding="utf-8")
    finally:
        db.close()


def test_both_kinds_can_be_granted_to_the_same_task(tmp_path):
    shared, _ = shared_tree(tmp_path)
    (shared / "sap.csv").write_text("year,amount\n", encoding="utf-8")
    provisioner = WorkspaceProvisioner(policy_for(shared, writable=True))
    workspace = tmp_path / "ws"

    manifest = provisioner.provision(
        workspace,
        [
            ResourceGrant(uri="file:spec.md", reason="読むだけ"),
            ResourceGrant(uri="file:sap.csv", access=AccessMode.READ_WRITE,
                          reason="書き換える"),
        ],
    )

    assert manifest.readable_paths == [str(shared / "spec.md")]
    assert manifest.writable_paths == [str(workspace / "resources/sap.csv")]
    assert (workspace / "resources" / "sap.csv").is_file()
    assert not (workspace / "resources" / "spec.md").exists()


def test_a_stored_grant_never_carries_its_own_delivery(tmp_path):
    """Delivery follows the access, so an old record cannot disagree with it."""
    stored = ResourceGrant.from_dict(
        {"uri": "file:spec.md", "access": "read", "delivery": "copy"}
    )

    assert stored.delivery is Delivery.REFERENCE

    shared, _ = shared_tree(tmp_path)
    manifest = WorkspaceProvisioner(policy_for(shared)).provision(
        tmp_path / "ws", [stored]
    )
    assert manifest.entries[0]["path"] == str(shared / "spec.md")


def test_a_task_can_narrow_what_it_reaches_but_never_widen_it(tmp_path):
    shared, _ = shared_tree(tmp_path)
    manifest = WorkspaceProvisioner(policy_for(shared)).provision(
        tmp_path / "ws",
        [ResourceGrant(uri="file:spec.md"), ResourceGrant(uri="file:plan.md")],
    )

    narrowed = manifest.narrowed_to(["file:spec.md", "file:never-granted.txt"])

    assert narrowed.readable_paths == [str(shared / "spec.md")]
    assert manifest.narrowed_to(None) is manifest
    assert manifest.narrowed_to([]).entries == []
