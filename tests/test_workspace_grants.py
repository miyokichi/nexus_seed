"""Task workspaces: what is granted, what is refused, what comes back.

The boundary these tests pin is not "the Agent behaves". It is that NEXUS SEED
decides, and that a task cannot reach past what it was given even if the Agent
ignores every instruction it was sent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


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

#: These tests are about the isolating pattern, so they always say `copy`.
COPY = Delivery.COPY


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
    (shared / "sneaky.txt").symlink_to(outside / "keys.txt")

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
            ResourceGrant(uri="file:spec.md", reason="参照用", delivery=COPY),
            ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE, delivery=COPY),
        ],
    )

    assert [entry["path"] for entry in manifest.entries] == [
        "resources/spec.md",
        "resources/plan.md",
    ]
    assert (workspace / "resources/spec.md").read_text(encoding="utf-8") == "共通仕様"
    # `writable_paths` is the *in-place* list an Agent runtime is authorized
    # with; a copy already sits inside the workspace it was given.
    assert manifest.writable_paths == []
    assert manifest.copied_writable_paths == ["resources/plan.md"]


def test_a_refused_grant_is_simply_not_provisioned(tmp_path):
    shared, outside = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))

    manifest = provisioner.provision(
        tmp_path / "ws",
        [
            ResourceGrant(uri="file:spec.md", delivery=COPY),
            ResourceGrant(uri=f"file:{outside / 'keys.txt'}", delivery=COPY),
        ],
    )

    assert [entry["uri"] for entry in manifest.entries] == ["file:spec.md"]
    assert not (tmp_path / "ws/resources/keys.txt").exists()


def test_a_grant_is_rechecked_when_the_root_narrows(tmp_path):
    """A stored grant must not outlive the permission it was made under."""
    shared, _ = shared_tree(tmp_path)
    grant = ResourceGrant(uri="file:spec.md", access=AccessMode.READ_WRITE, delivery=COPY)
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
        [ResourceGrant(uri="file:spec.md", delivery=COPY), ResourceGrant(uri="file:sub/spec.md", delivery=COPY)],
    )

    paths = [entry["path"] for entry in manifest.entries]
    assert paths == ["resources/spec.md", "resources/spec-1.md"]
    assert (tmp_path / "ws/resources/spec-1.md").read_text(encoding="utf-8") == "別の仕様"


def test_an_explicit_name_is_what_the_agent_sees(tmp_path):
    shared, _ = shared_tree(tmp_path)
    manifest = WorkspaceProvisioner(policy_for(shared)).provision(
        tmp_path / "ws", [ResourceGrant(uri="file:spec.md", name="仕様.md", delivery=COPY)]
    )
    assert manifest.entries[0]["path"] == "resources/仕様.md"


def test_the_workspace_says_what_is_in_it_two_ways(tmp_path):
    shared, _ = shared_tree(tmp_path)
    workspace = tmp_path / "ws"
    WorkspaceProvisioner(policy_for(shared)).provision(
        workspace, [ResourceGrant(uri="file:spec.md", reason="参照用", delivery=COPY)]
    )

    manifest = json.loads((workspace / MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest["resources"][0]["path"] == "resources/spec.md"

    # An Agent that reads files but not A2A metadata still learns what it has.
    readme = (workspace / README_PATH).read_text(encoding="utf-8")
    assert "resources/spec.md" in readme
    assert "read-only" in readme
    assert "NEED_RESOURCE" in readme


def test_the_manifest_never_names_the_host_path(tmp_path):
    """The Agent is told what it has, not where it came from."""
    shared, _ = shared_tree(tmp_path)
    workspace = tmp_path / "ws"
    WorkspaceProvisioner(policy_for(shared)).provision(
        workspace, [ResourceGrant(uri="file:spec.md", delivery=COPY)]
    )

    manifest = (workspace / MANIFEST_PATH).read_text(encoding="utf-8")
    assert str(shared) not in manifest


# --- the guarantee a read grant actually carries -------------------------------


def test_a_read_grant_is_written_read_only(tmp_path):
    shared, _ = shared_tree(tmp_path)
    workspace = tmp_path / "ws"
    WorkspaceProvisioner(policy_for(shared)).provision(
        workspace, [ResourceGrant(uri="file:spec.md", delivery=COPY)]
    )
    mode = (workspace / "resources/spec.md").stat().st_mode & 0o777
    assert mode == 0o444


def test_a_read_grant_never_reaches_the_original_even_if_overwritten(tmp_path):
    """The real boundary: a read grant is a copy, and nothing carries it back.

    The read-only file mode is a signal, not the protection — root and the
    file's owner can write it regardless. What holds either way is that the
    original never changes.
    """
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    grants = [ResourceGrant(uri="file:spec.md", delivery=COPY)]
    provisioner.provision(workspace, grants)

    copy = workspace / "resources/spec.md"
    os.chmod(copy, 0o644)  # an Agent that can write it anyway
    copy.write_text("改竄された内容", encoding="utf-8")

    assert provisioner.collect(workspace, grants) == []
    assert (shared / "spec.md").read_text(encoding="utf-8") == "共通仕様"


def test_a_read_write_grant_is_carried_back_when_work_is_accepted(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"
    grants = [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE, delivery=COPY)]
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
        workspace, [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE, delivery=COPY)]
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
        workspace, [ResourceGrant(uri=f"knowledge:{item.knowledge_id}", delivery=COPY)]
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
    provisioner.provision(workspace, [ResourceGrant(uri="file:spec.md", delivery=COPY)])
    (workspace / "notes.md").write_text("Agentの作業メモ", encoding="utf-8")

    manifest = provisioner.provision(
        workspace,
        [ResourceGrant(uri="file:spec.md", delivery=COPY), ResourceGrant(uri="file:plan.md", delivery=COPY)],
    )

    assert [entry["path"] for entry in manifest.entries] == [
        "resources/spec.md",
        "resources/plan.md",
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


# --- delivered by reference: the original, not a duplicate of it -------------


def test_a_referenced_file_is_not_copied_anywhere(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(policy_for(shared))
    workspace = tmp_path / "ws"

    manifest = provisioner.provision(workspace, [ResourceGrant(uri="file:spec.md")])

    assert manifest.entries[0]["path"] == str(shared / "spec.md")
    assert manifest.entries[0]["delivery"] == "reference"
    assert manifest.readable_paths == [str(shared / "spec.md")]
    assert list((workspace / "resources").iterdir()) == []


def test_writing_a_referenced_file_changes_the_original_immediately(tmp_path):
    shared, _ = shared_tree(tmp_path)
    provisioner = WorkspaceProvisioner(
        policy_for(shared, writable=True)
    )
    grants = [ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE)]

    manifest = provisioner.provision(tmp_path / "ws", grants)
    # This is what the Agent does with the path it was handed.
    Path(manifest.writable_paths[0]).write_text("計画v2", encoding="utf-8")

    assert (shared / "plan.md").read_text(encoding="utf-8") == "計画v2"
    # Nothing to carry back: it already landed where it belongs.
    assert provisioner.collect(tmp_path / "ws", grants) == []


def test_a_directory_can_be_referenced_but_never_copied(tmp_path):
    shared, _ = shared_tree(tmp_path)
    (shared / "sub").mkdir(exist_ok=True)
    policy = policy_for(shared)

    allowed = policy.decide(GrantRequest(project_id="p", requested="sub", uri="file:sub"))
    refused = policy.decide(
        GrantRequest(project_id="p", requested="sub", uri="file:sub"),
        delivery=Delivery.COPY,
    )

    assert allowed.allowed is True
    assert refused.allowed is False
    assert "directory" in refused.reason
    manifest = WorkspaceProvisioner(policy).provision(
        tmp_path / "ws", [ResourceGrant(uri="file:sub")]
    )
    assert manifest.readable_paths == [str(shared / "sub")]


def test_a_referenced_path_still_cannot_escape_the_authorized_root(tmp_path):
    shared, outside = shared_tree(tmp_path)
    (shared / "sneaky.txt").symlink_to(outside / "keys.txt")
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


def test_a_record_has_no_host_path_so_it_can_only_be_copied(tmp_path):
    shared, _ = shared_tree(tmp_path)
    db = Database(str(tmp_path / "k.db"))
    try:
        ledger = KnowledgeLedger(KnowledgeStore(db))
        item = ledger.record("決まったこと", source_type="meeting")
        provisioner = WorkspaceProvisioner(policy_for(shared, ledger=ledger))

        manifest = provisioner.provision(
            tmp_path / "ws",
            [
                ResourceGrant(uri=f"knowledge:{item.knowledge_id}"),
                ResourceGrant(uri=f"knowledge:{item.knowledge_id}", delivery=COPY),
            ],
        )

        assert [entry["delivery"] for entry in manifest.entries] == ["copy"]
        assert manifest.readable_paths == []
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
            ResourceGrant(uri="file:sap.csv", access=AccessMode.READ_WRITE, reason="直接更新"),
            ResourceGrant(uri="file:plan.md", access=AccessMode.READ_WRITE,
                          delivery=COPY, reason="編集させるが原本は守る"),
        ],
    )

    assert manifest.readable_paths == [str(shared / "spec.md")]
    assert manifest.writable_paths == [str(shared / "sap.csv")]
    assert manifest.copied_writable_paths == ["resources/plan.md"]
    assert (workspace / "resources" / "plan.md").is_file()
    assert not (workspace / "resources" / "spec.md").exists()


def test_a_stored_grant_from_before_deliveries_still_means_copy(tmp_path):
    # Read back from a Project saved before this existed: it copied, and
    # reading the record must not quietly change that.
    grant = ResourceGrant.from_dict({"uri": "file:spec.md", "access": "read"})

    assert grant.delivery is Delivery.COPY

    shared, _ = shared_tree(tmp_path)
    manifest = WorkspaceProvisioner(policy_for(shared)).provision(
        tmp_path / "ws", [grant]
    )
    assert manifest.entries[0]["path"] == "resources/spec.md"


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
