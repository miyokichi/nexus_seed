"""SQLite persistence for provider federation, delegation and skill imports."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from ..core.event import utcnow
from ..providers.models import (
    ExecutionProvider,
    ImportedSkill,
    ProviderBinding,
    ProviderHealth,
    ProviderInvocation,
    ProviderInvocationStatus,
    ProviderKind,
    ProviderSelection,
    ProviderStatus,
    SkillDescriptor,
)
from .database import Database


def _dumps(value) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _loads(value, default):
    return json.loads(value) if value else default


class ProviderStore:
    """Durable read/write side of ProviderRegistry."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_provider(self, provider: ExecutionProvider) -> ExecutionProvider:
        existing = self.get_provider_by_name(provider.name, provider.version)
        if existing is not None:
            provider.id = existing.id
            provider.created_at = existing.created_at
        provider.updated_at = utcnow()
        self.db.execute(
            """INSERT INTO execution_providers
               (id,name,version,kind,status,health,adapter_name,adapter_config_json,
                declared_permissions_json,priority,estimated_cost,estimated_latency,
                trust_level,metadata_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status, health=excluded.health,
                 adapter_name=excluded.adapter_name,
                 adapter_config_json=excluded.adapter_config_json,
                 declared_permissions_json=excluded.declared_permissions_json,
                 priority=excluded.priority, estimated_cost=excluded.estimated_cost,
                 estimated_latency=excluded.estimated_latency,
                 trust_level=excluded.trust_level, metadata_json=excluded.metadata_json,
                 updated_at=excluded.updated_at""",
            (str(provider.id), provider.name, provider.version, provider.kind.value,
             provider.status.value, provider.health.value, provider.adapter_name,
             _dumps(provider.adapter_config), _dumps(provider.declared_permissions),
             provider.priority, provider.estimated_cost, provider.estimated_latency,
             provider.trust_level, _dumps(provider.metadata),
             provider.created_at.isoformat(), provider.updated_at.isoformat()),
        )
        return provider

    def get_provider(self, provider_id) -> ExecutionProvider | None:
        row = self.db.query_one("SELECT * FROM execution_providers WHERE id=?", (str(provider_id),))
        return self._provider(row) if row else None

    def get_provider_by_name(self, name: str, version: str) -> ExecutionProvider | None:
        row = self.db.query_one(
            "SELECT * FROM execution_providers WHERE name=? AND version=?", (name, version)
        )
        return self._provider(row) if row else None

    def all_providers(self) -> list[ExecutionProvider]:
        return [self._provider(r) for r in self.db.query(
            "SELECT * FROM execution_providers ORDER BY name, version"
        )]

    def update_provider(self, provider_id, *, status=None, health=None) -> None:
        provider = self.get_provider(provider_id)
        if provider is None:
            return
        if status is not None:
            provider.status = ProviderStatus(status)
        if health is not None:
            provider.health = ProviderHealth(health)
        self.save_provider(provider)

    def save_binding(self, binding: ProviderBinding) -> ProviderBinding:
        existing = self.find_binding(binding.definition_key, binding.provider_id)
        if existing is not None:
            binding.id = existing.id
            binding.created_at = existing.created_at
        binding.updated_at = utcnow()
        self.db.execute(
            """INSERT INTO provider_bindings
               (id,process_definition_name,process_definition_version,provider_id,
                priority,enabled,required_permissions_json,metadata_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET priority=excluded.priority,
                 enabled=excluded.enabled,
                 required_permissions_json=excluded.required_permissions_json,
                 metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (str(binding.id), binding.process_definition_name,
             binding.process_definition_version, str(binding.provider_id), binding.priority,
             int(binding.enabled), _dumps(binding.required_permissions),
             _dumps(binding.metadata), binding.created_at.isoformat(),
             binding.updated_at.isoformat()),
        )
        return binding

    def find_binding(self, definition_key, provider_id) -> ProviderBinding | None:
        row = self.db.query_one(
            """SELECT * FROM provider_bindings WHERE process_definition_name=?
               AND process_definition_version=? AND provider_id=?""",
            (definition_key[0], definition_key[1], str(provider_id)),
        )
        return self._binding(row) if row else None

    def bindings_for(self, name: str, version: str) -> list[ProviderBinding]:
        return [self._binding(r) for r in self.db.query(
            """SELECT * FROM provider_bindings WHERE process_definition_name=?
               AND process_definition_version=? ORDER BY priority DESC, provider_id ASC""",
            (name, version),
        )]

    def all_bindings(self) -> list[ProviderBinding]:
        return [self._binding(r) for r in self.db.query(
            "SELECT * FROM provider_bindings ORDER BY process_definition_name, process_definition_version"
        )]

    def save_selection(self, selection: ProviderSelection) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO provider_selections
               (id,process_instance_id,activation_id,process_definition_name,
                process_definition_version,provider_id,provider_binding_id,
                eligible_provider_ids_json,reasons_json,selected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(selection.id), str(selection.process_instance_id), selection.activation_id,
             selection.process_definition_name, selection.process_definition_version,
             str(selection.provider_id) if selection.provider_id else None,
             str(selection.provider_binding_id) if selection.provider_binding_id else None,
             _dumps(selection.eligible_provider_ids), _dumps(selection.reasons),
             selection.selected_at.isoformat()),
        )

    def selections_for_instance(self, instance_id) -> list[ProviderSelection]:
        return [self._selection(r) for r in self.db.query(
            "SELECT * FROM provider_selections WHERE process_instance_id=? ORDER BY selected_at",
            (str(instance_id),),
        )]

    def all_selections(self) -> list[ProviderSelection]:
        """Return the complete provider-selection audit in chronological order."""
        return [
            self._selection(row)
            for row in self.db.query(
                "SELECT * FROM provider_selections ORDER BY selected_at"
            )
        ]

    def save_invocation(self, invocation: ProviderInvocation) -> ProviderInvocation:
        self.db.execute(
            """INSERT INTO provider_invocations
               (id,provider_id,provider_binding_id,process_instance_id,plan_node_id,
                request_snapshot_json,status,external_run_id,attempt,idempotency_key,
                result_snapshot_json,started_at,completed_at,error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                 external_run_id=excluded.external_run_id,
                 result_snapshot_json=excluded.result_snapshot_json,
                 completed_at=excluded.completed_at,error=excluded.error""",
            (str(invocation.id), str(invocation.provider_id),
             str(invocation.provider_binding_id), str(invocation.process_instance_id),
             str(invocation.plan_node_id) if invocation.plan_node_id else None,
             _dumps(invocation.request_snapshot), invocation.status.value,
             invocation.external_run_id, invocation.attempt, invocation.idempotency_key,
             _dumps(invocation.result_snapshot) if invocation.result_snapshot is not None else None,
             invocation.started_at.isoformat(),
             invocation.completed_at.isoformat() if invocation.completed_at else None,
             invocation.error),
        )
        return invocation

    def get_invocation(self, invocation_id) -> ProviderInvocation | None:
        row = self.db.query_one("SELECT * FROM provider_invocations WHERE id=?", (str(invocation_id),))
        return self._invocation(row) if row else None

    def invocation_for_key(self, key: str) -> ProviderInvocation | None:
        row = self.db.query_one("SELECT * FROM provider_invocations WHERE idempotency_key=?", (key,))
        return self._invocation(row) if row else None

    def invocations(self, *, provider_id=None, status=None) -> list[ProviderInvocation]:
        sql, params = "SELECT * FROM provider_invocations", []
        clauses = []
        if provider_id is not None:
            clauses.append("provider_id=?"); params.append(str(provider_id))
        if status is not None:
            clauses.append("status=?"); params.append(ProviderInvocationStatus(status).value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at"
        return [self._invocation(r) for r in self.db.query(sql, tuple(params))]

    def save_imported_skill(self, imported: ImportedSkill) -> ImportedSkill:
        existing = self.db.query_one(
            """SELECT * FROM imported_skills WHERE source=?
               AND process_definition_name=? AND process_definition_version=?""",
            (
                imported.source,
                imported.process_definition_name,
                imported.process_definition_version,
            ),
        )
        if existing is not None:
            prior = self._imported_skill(existing)
            imported.id = prior.id
            imported.created_at = prior.created_at
        imported.updated_at = utcnow()
        self.db.execute(
            """INSERT INTO imported_skills
               (id,source,descriptor_json,status,provider_id,process_definition_name,
                process_definition_version,reasons_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                 provider_id=excluded.provider_id,
                 process_definition_name=excluded.process_definition_name,
                 process_definition_version=excluded.process_definition_version,
                 reasons_json=excluded.reasons_json,updated_at=excluded.updated_at""",
            (str(imported.id), imported.source, _dumps(imported.descriptor.to_dict()),
             imported.status, str(imported.provider_id) if imported.provider_id else None,
             imported.process_definition_name, imported.process_definition_version,
             _dumps(imported.reasons), imported.created_at.isoformat(),
             imported.updated_at.isoformat()),
        )
        return imported

    def imported_skills(self) -> list[ImportedSkill]:
        return [self._imported_skill(r) for r in self.db.query(
            "SELECT * FROM imported_skills ORDER BY created_at"
        )]

    @staticmethod
    def _provider(row):
        return ExecutionProvider(
            id=uuid.UUID(row["id"]), name=row["name"], version=row["version"],
            kind=ProviderKind(row["kind"]), status=ProviderStatus(row["status"]),
            health=ProviderHealth(row["health"]), adapter_name=row["adapter_name"],
            adapter_config=_loads(row["adapter_config_json"], {}),
            declared_permissions=tuple(_loads(row["declared_permissions_json"], [])),
            priority=row["priority"], estimated_cost=row["estimated_cost"],
            estimated_latency=row["estimated_latency"], trust_level=row["trust_level"],
            metadata=_loads(row["metadata_json"], {}),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _binding(row):
        return ProviderBinding(
            id=uuid.UUID(row["id"]), process_definition_name=row["process_definition_name"],
            process_definition_version=row["process_definition_version"],
            provider_id=uuid.UUID(row["provider_id"]), priority=row["priority"],
            enabled=bool(row["enabled"]),
            required_permissions=tuple(_loads(row["required_permissions_json"], [])),
            metadata=_loads(row["metadata_json"], {}),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _selection(row):
        return ProviderSelection(
            id=uuid.UUID(row["id"]), process_instance_id=uuid.UUID(row["process_instance_id"]),
            activation_id=row["activation_id"],
            process_definition_name=row["process_definition_name"],
            process_definition_version=row["process_definition_version"],
            provider_id=uuid.UUID(row["provider_id"]) if row["provider_id"] else None,
            provider_binding_id=uuid.UUID(row["provider_binding_id"]) if row["provider_binding_id"] else None,
            eligible_provider_ids=[uuid.UUID(v) for v in _loads(row["eligible_provider_ids_json"], [])],
            reasons=_loads(row["reasons_json"], []),
            selected_at=datetime.fromisoformat(row["selected_at"]),
        )

    @staticmethod
    def _invocation(row):
        return ProviderInvocation(
            id=uuid.UUID(row["id"]), provider_id=uuid.UUID(row["provider_id"]),
            provider_binding_id=uuid.UUID(row["provider_binding_id"]),
            process_instance_id=uuid.UUID(row["process_instance_id"]),
            plan_node_id=uuid.UUID(row["plan_node_id"]) if row["plan_node_id"] else None,
            request_snapshot=_loads(row["request_snapshot_json"], {}),
            status=ProviderInvocationStatus(row["status"]),
            external_run_id=row["external_run_id"], attempt=row["attempt"],
            idempotency_key=row["idempotency_key"],
            result_snapshot=_loads(row["result_snapshot_json"], None),
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            error=row["error"],
        )

    @staticmethod
    def _imported_skill(row):
        data = _loads(row["descriptor_json"], {})
        descriptor = SkillDescriptor(**data)
        return ImportedSkill(
            id=uuid.UUID(row["id"]), source=row["source"], descriptor=descriptor,
            status=row["status"],
            provider_id=uuid.UUID(row["provider_id"]) if row["provider_id"] else None,
            process_definition_name=row["process_definition_name"],
            process_definition_version=row["process_definition_version"],
            reasons=_loads(row["reasons_json"], []),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
