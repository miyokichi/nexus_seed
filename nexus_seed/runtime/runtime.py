"""Runtime — the NEXUS SEED event-driven engine.

The Runtime is *mechanism only*.  It holds no domain knowledge and makes no
"AI" decisions; all meaning lives in process handlers.  Its responsibilities:

    store events, route events, create processes, run processes, suspend
    processes, persist/match continuations, resume processes, persist state.

All durable state lives in SQLite, so a Runtime can be discarded and rebuilt
from the same database with no loss of process state.

Phase 2A makes the runtime *durable*:

* activations commit atomically (see :mod:`.executor`);
* duplicate events are ignored (idempotent intake);
* :meth:`recover` returns crash-orphaned RUNNING processes to RUNNABLE;
* :meth:`tick` fires due timers and promotes due retries;
* processes can spawn children and join on them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..core.event import Event
from ..core.process import (
    Handler,
    HandlerRegistry,
    ProcessDefinition,
    ProcessInstance,
    ProcessStatus,
)
from ..storage.activation_store import ActivationStore
from ..storage.continuation_store import ContinuationStore
from ..storage.database import Database
from ..storage.event_store import EventStore
from ..storage.join_store import JoinStore
from ..storage.observation_store import ObservationStore
from ..storage.process_store import ProcessStore
from ..storage.state_delta_store import StateDeltaStore
from ..storage.state_store import StateStore
from ..storage.timer_store import TimerStore
from ..storage.work_requirement_store import WorkRequirementStore
from ..storage.context_snapshot_store import ContextSnapshotStore
from ..storage.proposal_store import ProposalStore
from ..storage.llm_invocation_store import LLMInvocationStore
from ..storage.action_proposal_store import ActionProposalStore
from ..storage.action_execution_store import ActionExecutionStore
from ..storage.action_decision_store import ActionDecisionStore
from ..storage.ingress_receipt_store import IngressReceiptStore
from ..storage.adapter_checkpoint_store import AdapterCheckpointStore
from ..storage.chat_store import ProjectChatStore
from ..storage.resource_store import ResourceStore
from ..storage.event_delivery_store import EventDeliveryStore
from ..delivery.dispatcher import DurableEventDispatcher
from ..delivery.models import EventDelivery, EventDeliveryStatus
from ..storage.capability_store import CapabilityStore
from ..capabilities.matcher import CapabilityMatcher
from ..capabilities.models import Capability, CapabilityRequirement, CapabilityWorkMatch
from ..capabilities.registry import CapabilityRegistry, as_refs
from ..capabilities.trace import CapabilityTrace, get_capability_trace
from ..storage.plan_store import PlanStore
from ..planning.models import PlanNode, PlanStatus, ProcessPlan
from ..planning.planner import CompositionPlanner
from ..planning.trace import PlanTrace, get_plan_trace
from ..planning.validation import PlanValidator
from ..decision.evaluator import PlanEvaluator
from ..decision.policy import PlanSelectionPolicy
from ..decision.selector import DeterministicPlanSelector
from ..decision.trace import DecisionTrace, get_decision_trace
from ..decision.validation import SelectionValidator
from ..storage.decision_store import DecisionStore
from ..storage.extension_store import ExtensionStore
from ..storage.construction_store import ConstructionStore
from ..storage.installation_store import InstallationStore
from ..storage.autonomy_store import AutonomyStore
from ..storage.provider_store import ProviderStore
from ..storage.control_store import ControlStore
from ..control.service import ConsoleService
from ..providers.models import ExecutionProvider, ProviderBinding
from ..providers.registry import ProviderRegistry
from ..providers.skills import DirectorySkillAdapter, SkillImporter
from ..providers.trace import ProviderTrace, get_provider_trace
from ..construction.generator import LLMConstructionGenerator
from ..construction.planner import ConstructionPlanner
from ..construction.validator import ConstructionValidator
from ..construction.workspace import (
    BACKEND_NAME as CONSTRUCTION_BACKEND_NAME,
    ConstructionActionBackend,
    SandboxWorkspaceManager,
)
from ..construction.trace import ConstructionTrace, get_construction_trace
from ..installation.planner import InstallationPlanner
from ..installation.policy import InstallationPolicy
from ..installation.validator import InstallationValidator
from ..installation.workspace import (
    BACKEND_NAME as INSTALLATION_BACKEND_NAME,
    InstallationActionBackend,
    ProductionInstallationManager,
)
from ..installation.trace import InstallationTrace, get_installation_trace
from ..autonomy.models import AcquisitionStatus, AutonomyBudget
from ..autonomy.policy import AutonomyPolicy
from ..autonomy.trace import AcquisitionTrace, get_acquisition_trace
from ..extension.analyzer import (
    AcquisitionEnvironment,
    CapabilityAcquisitionAnalyzer,
    PluginCatalog,
)
from ..extension.models import (
    CapabilityGap,
    CapabilityGapStatus,
    ExtensionDecisionRecord,
    ExtensionProposal,
    ExtensionProposalStatus,
)
from ..extension.policy import ExtensionPolicy
from ..extension.trace import (
    CapabilityGapTrace,
    ExtensionTrace,
    get_capability_gap_trace,
    get_extension_trace,
)
from ..extension.validator import ExtensionValidator
from ..context.compiler import ContextCompiler
from ..context.models import ContextSnapshot
from ..backends.action import capabilities_of
from ..backends.base import ExecutionBackend, LLMInvocation
from ..intelligence.proposal import InterpretationProposal
from ..actions.models import (
    ActionDecisionRecord,
    ActionExecution,
    ActionProposal,
    ActionProposalStatus,
)
from ..actions.trace import ActionTrace, get_action_trace
from ..ingress.models import AdapterCheckpoint, IngressReceipt
from ..ingress.service import AdapterRegistry, IngressService
from ..ingress.trace import (
    IngressTrace,
    get_ingress_trace,
    get_ingress_trace_by_source_key,
)
from ..resources.extractors import default_registry
from ..resources.models import Resource, ResourceRepresentation, ResourceVersion
from ..resources.scope import ResourceScope
from ..resources.service import ResourceService
from ..resources.trace import (
    RepresentationTrace,
    ResourceTrace,
    get_representation_trace,
    get_resource_trace,
)
from ..core.state import StateEntry, StateHistoryEntry
from ..world.provenance import Provenance, get_state_provenance
from ..work.trace import WorkTrace, get_work_trace
from ..work.work_requirement import WorkRequirement, WorkStatus
from .clock import Clock
from .drain import UNLIMITED, DrainBudget, DrainResult
from .services import RuntimeServices
from .continuation_resolver import ContinuationResolver
from .executor import Executor
from .join_coordinator import JoinCoordinator
from .router import Router
from .scheduler import Scheduler

logger = logging.getLogger("nexus_seed.runtime")


class Runtime:
    """Wires together stores, router, scheduler, executor and coordinators.

    Handlers are code and must be (re)registered on every construction via
    :meth:`register_process`; definitions and all other state are loaded from
    the SQLite database at ``db_path``.  Construction runs a crash-recovery
    sweep automatically.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        registry: HandlerRegistry | None = None,
        clock: Clock | None = None,
        construction_root: str | Path | None = None,
        installation_root: str | Path | None = None,
        control_enabled: bool = True,
    ) -> None:
        self.db = Database(db_path)
        self.clock = clock or Clock()

        # Delivery obligations are created by the event store itself, so no
        # call site can persist an event without promising to route it.
        self.event_delivery_store = EventDeliveryStore(self.db)
        self.event_store = EventStore(self.db, self.event_delivery_store)
        self.process_store = ProcessStore(self.db)
        self.continuation_store = ContinuationStore(self.db)
        self.state_store = StateStore(self.db)
        self.timer_store = TimerStore(self.db)
        self.join_store = JoinStore(self.db)
        self.activation_store = ActivationStore(self.db)
        self.observation_store = ObservationStore(self.db)
        self.state_delta_store = StateDeltaStore(self.db)
        self.work_requirement_store = WorkRequirementStore(self.db)
        self.control_store = ControlStore(self.db)
        #: Domains register ``(name, callable)`` here to say what NEXUS SEED is
        #: actively pursuing.  Phase 6 asks the Runtime, not the Control Plane,
        #: so what is being pursued can change source without touching Phase 6.
        self.pursuit_sources: list = []
        #: Domains register ``(name, callable)`` here to commit their own
        #: records inside a process activation's transaction.  The Runtime does
        #: not know what those records mean — this is the seam that lets a
        #: domain (the Control Plane first) be removed without editing the
        #: executor.
        self.result_appliers: list = []
        self.context_snapshot_store = ContextSnapshotStore(self.db)
        self.proposal_store = ProposalStore(self.db)
        self.llm_invocation_store = LLMInvocationStore(self.db)
        self.action_proposal_store = ActionProposalStore(self.db)
        self.action_execution_store = ActionExecutionStore(self.db)
        self.action_decision_store = ActionDecisionStore(self.db)
        self.ingress_receipt_store = IngressReceiptStore(self.db)
        self.adapter_checkpoint_store = AdapterCheckpointStore(self.db)
        #: The human-facing Project Chat journal.  Read by the interface layer
        #: only; no Process, Router or Executor path touches it.
        self.chat_store = ProjectChatStore(self.db)
        self.resource_store = ResourceStore(self.db)
        self.backends: dict[str, ExecutionBackend] = {}
        self.adapters = AdapterRegistry()
        # The ingress boundary, available to observer processes.  Constructed
        # eagerly so `runtime.ingress` is always usable; it holds no state of
        # its own beyond the stores it reads.
        self.ingress = IngressService(self)
        self.resource_scope: ResourceScope | None = None
        #: Set by application wiring when requests are routed to Projects.
        self.project_orchestrator = None
        self.resource_service = ResourceService(self.resource_store)
        #: Extractors available to extraction processes.  Adding a format is a
        #: registration here, never a Runtime change (Invariant 38).
        self.extractors = default_registry()
        # What the system can do — its self-model, kept apart from World State
        # (which is what it knows about the outside).
        self.capability_store = CapabilityStore(self.db)
        self.capabilities = CapabilityRegistry(self.capability_store)
        self.provider_store = ProviderStore(self.db)
        self.providers = ProviderRegistry(self.provider_store, self.capabilities)
        for persisted_definition in self.process_store.all_definitions():
            if not (persisted_definition.metadata or {}).get("external_provider_only"):
                self.providers.ensure_internal_binding(persisted_definition)
        self.capability_matcher = CapabilityMatcher(
            self.capabilities, self.providers
        )
        self.plan_store = PlanStore(self.db)
        self.composition_planner = CompositionPlanner(
            self.capabilities, provider_registry=self.providers
        )
        self.plan_validator = PlanValidator(
            self.capabilities, provider_registry=self.providers
        )
        # Phase 4C: composing a plan and choosing one are separate jobs
        # (Invariant 75), so the pieces that do the choosing are separate too.
        self.decision_store = DecisionStore(self.db)
        self.plan_evaluator = PlanEvaluator()
        self.plan_selector = DeterministicPlanSelector()
        self.selection_validator = SelectionValidator(self.plan_validator)
        self.selection_policy = PlanSelectionPolicy()
        #: Which selector the decision process should ask.  ``None`` means the
        #: deterministic one alone, which must always be enough (Invariant 77).
        self.llm_plan_selector = None
        #: How many times a need may be replanned when its work does not say.
        self.default_max_replans = 3
        # Phase 5A: what the system *cannot* do, and what it proposes about it.
        # Kept beside the capability registry rather than inside it — the
        # registry is the self-model, these are its acknowledged holes.
        self.extension_store = ExtensionStore(self.db)
        self.acquisition_analyzer = CapabilityAcquisitionAnalyzer(self.capabilities)
        self.extension_validator = ExtensionValidator(
            self.capabilities, matcher=self.capability_matcher
        )
        self.extension_policy = ExtensionPolicy()
        #: Known external plugins, as static data — never a marketplace client.
        self.plugin_catalog = PluginCatalog()
        #: Optional model that elaborates an analyzed route.  ``None`` means the
        #: deterministic builder alone, which must always be enough (spec §82).
        self.llm_extension_proposer = None
        # Phase 5B construction is durable data plus an isolated file boundary.
        self.construction_store = ConstructionStore(self.db)
        self.construction_planner = ConstructionPlanner()
        self.construction_validator = ConstructionValidator()
        self.workspace_manager = SandboxWorkspaceManager(construction_root)
        repository_root = Path.cwd().resolve()
        if repository_root == self.workspace_manager.base_root or repository_root in self.workspace_manager.base_root.parents:
            raise ValueError("construction_root must not be the production repository")
        self.llm_construction_generator: LLMConstructionGenerator | None = None
        self.backends[CONSTRUCTION_BACKEND_NAME] = ConstructionActionBackend(
            self.workspace_manager, self.construction_store
        )
        # Phase 5C production data has a separate versioned root and authority.
        self.installation_store = InstallationStore(self.db)
        self.installation_planner = InstallationPlanner()
        self.installation_validator = InstallationValidator()
        self.installation_policy = InstallationPolicy()
        if installation_root is None and str(db_path) != ":memory:":
            installation_root = Path(db_path).resolve().parent / "installed_extensions"
        self.installation_manager = ProductionInstallationManager(installation_root)
        if repository_root == self.installation_manager.root or repository_root in self.installation_manager.root.parents:
            raise ValueError("installation_root must not be the production repository source tree")
        self.backends[INSTALLATION_BACKEND_NAME] = InstallationActionBackend(
            self.installation_manager, self.installation_store, self.resource_store
        )
        # Phase 5D coordinates the existing extension/construction/installation
        # boundaries.  The policy is deterministic and cannot edit itself.
        self.autonomy_store = AutonomyStore(self.db)
        self.autonomy_policy = AutonomyPolicy()
        self.default_autonomy_budget = AutonomyBudget()

        # Phase 5G application service.  It is outside Runtime mechanism even
        # though Runtime exposes the wired instance to CLI/HTTP adapters.
        #
        # The Control Plane is being wound down in favour of the Project
        # Orchestrator, so it is switchable.  ``None`` removes the /control
        # endpoint, the Cockpit's control actions and the Control Plane
        # instruction box; every caller already guards for it.  Goals and the
        # durable runtime are untouched — this turns off the human command
        # surface, not the records behind it.
        self.console = ConsoleService(self) if control_enabled else None

        self.registry = registry or HandlerRegistry()
        self.resolver = ContinuationResolver(self.continuation_store, self.process_store)
        self.router = Router(self.process_store, self.resolver)
        self.scheduler = Scheduler(self.process_store)
        self.join_coordinator = JoinCoordinator(self.join_store, self.process_store)
        self.services = RuntimeServices(
            process_store=self.process_store,
            work_requirement_store=self.work_requirement_store,
            state_store=self.state_store,
            proposal_store=self.proposal_store,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
            resource_service=self.resource_service,
            capability_registry=self.capabilities,
            capability_matcher=self.capability_matcher,
            capability_store=self.capability_store,
            plan_store=self.plan_store,
            composition_planner=self.composition_planner,
            plan_validator=self.plan_validator,
            decision_store=self.decision_store,
            plan_evaluator=self.plan_evaluator,
            plan_selector=self.plan_selector,
            selection_validator=self.selection_validator,
            selection_policy=self.selection_policy,
            extension_store=self.extension_store,
            acquisition_analyzer=self.acquisition_analyzer,
            extension_validator=self.extension_validator,
            construction_store=self.construction_store,
            construction_planner=self.construction_planner,
            construction_validator=self.construction_validator,
            workspace_manager=self.workspace_manager,
            installation_store=self.installation_store,
            installation_planner=self.installation_planner,
            installation_validator=self.installation_validator,
            installation_policy=self.installation_policy,
            installation_manager=self.installation_manager,
            autonomy_store=self.autonomy_store,
            autonomy_policy=self.autonomy_policy,
            autonomy_budget=self.default_autonomy_budget,
            provider_registry=self.providers,
            continuation_store=self.continuation_store,
            control_store=self.control_store,
            runtime=self,
            extractor_registry=self.extractors,
            ingress_receipt_store=self.ingress_receipt_store,
        )
        self.context_compiler = ContextCompiler(
            process_store=self.process_store,
            event_store=self.event_store,
            state_store=self.state_store,
            observation_store=self.observation_store,
            state_delta_store=self.state_delta_store,
            work_requirement_store=self.work_requirement_store,
            continuation_store=self.continuation_store,
            resource_store=self.resource_store,
        )
        self.executor = Executor(
            self.db,
            self.registry,
            self.process_store,
            self.continuation_store,
            self.state_store,
            self.event_store,
            self.timer_store,
            self.join_store,
            self.activation_store,
            self.observation_store,
            self.state_delta_store,
            self.work_requirement_store,
            self.clock,
            self.context_compiler,
            self.context_snapshot_store,
            self.proposal_store,
            self.llm_invocation_store,
            self.backends,
            self.services,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
            action_decision_store=self.action_decision_store,
            resource_store=self.resource_store,
            adapters=self.adapters,
            ingress=self.ingress,
            project_orchestrator=None,
            capability_store=self.capability_store,
            plan_store=self.plan_store,
            decision_store=self.decision_store,
            extension_store=self.extension_store,
            construction_store=self.construction_store,
            installation_store=self.installation_store,
            installation_manager=self.installation_manager,
            autonomy_store=self.autonomy_store,
            provider_registry=self.providers,
            result_appliers=self.result_appliers,
        )
        #: How much one drain call may do.  Unlimited by default, so callers
        #: written before Phase 4B.1 behave exactly as they did.
        self.default_drain_budget: DrainBudget = UNLIMITED
        #: What the most recent drain slice did (operational visibility).
        self.last_drain: DrainResult | None = None
        self.dispatcher = DurableEventDispatcher(
            self.db,
            self.event_store,
            self.event_delivery_store,
            self.router,
            self.clock,
        )

        self.recover()

    # --- registration ------------------------------------------------------

    def register_process(
        self,
        definition: ProcessDefinition,
        handler: Handler,
        *,
        bind_internal: bool = True,
    ) -> None:
        """Persist ``definition``, bind its handler, and declare what it can do.

        If the definition introduces a capability that was not previously
        available, a ``capability_available`` event is appended.  It is only
        *appended* — Phase 3F's delivery obligation carries it to whoever cares
        (the blocked-work reconciler), and registering during startup therefore
        works even though nothing is draining yet.
        """
        self.process_store.upsert_definition(definition)
        self.registry.register(definition.handler, handler)
        if bind_internal:
            self.providers.ensure_internal_binding(definition)
        logger.info("registered process %s v%s", definition.name, definition.version)

        refs = as_refs(definition.provides_capabilities)
        if not refs:
            return
        newly_available = self.capabilities.declare(
            definition.name, definition.version, refs
        )
        for ref in newly_available:
            self.event_store.append(
                Event(
                    type="capability_available",
                    source="runtime.capabilities",
                    payload={
                        "capability_name": ref.name,
                        "capability_version": ref.version,
                        "definition_name": definition.name,
                        "definition_version": definition.version,
                    },
                )
            )

    def register_provider(self, provider: ExecutionProvider, adapter=None):
        """Register an execution provider and its optional runtime adapter."""
        saved = self.providers.register_provider(provider, adapter)
        return saved

    def register_provider_adapter(self, name: str, adapter) -> None:
        """Attach a protocol adapter after restart without changing records."""
        self.providers.register_adapter(name, adapter)

    def import_directory_skill(
        self,
        source,
        adapter: DirectorySkillAdapter,
        *,
        allowed_permissions: tuple[str, ...] = (),
    ):
        """Inspect, validate and register a directory-backed external skill."""
        return SkillImporter(self).import_directory(
            source, adapter, allowed_permissions=allowed_permissions
        )

    def import_skill_catalog(
        self,
        catalog,
        *,
        adapter: DirectorySkillAdapter | None = None,
        provider_for=None,
        allowed_permissions: tuple[str, ...] = (),
    ):
        """Register a loaded Skill catalog through the same safety pipeline.

        ``provider_for`` maps each Skill to an already-registered provider id,
        which is how a cognitive Skill reaches an external Agent Runtime
        without ever naming an endpoint itself.
        """
        return SkillImporter(self).import_catalog(
            catalog,
            adapter=adapter,
            provider_for=provider_for,
            allowed_permissions=allowed_permissions,
        )

    async def cancel_provider_invocation(self, invocation_id) -> bool:
        """Cancel one external delegation, telling the provider on a best-effort basis."""
        return await self.providers.cancel_invocation(invocation_id)

    def register_provider_binding(self, binding: ProviderBinding):
        """Bind a provider to one semantic ProcessDefinition.

        Availability is announced durably so provider-blocked work is
        reconciled without replaying its source event.
        """
        definition = self.process_store.get_definition(*binding.definition_key)
        had_eligible = bool(
            definition and self.providers.has_eligible_provider(definition)
        )
        saved = self.providers.register_binding(binding)
        provider = self.provider_store.get_provider(saved.provider_id)
        definition = self.process_store.get_definition(*saved.definition_key)
        if provider is not None and definition is not None:
            if not had_eligible and self.providers.has_eligible_provider(definition):
                self.event_store.append(
                    Event(
                        type="provider_available",
                        source="runtime.providers",
                        payload={
                            "provider_id": str(provider.id),
                            "definition_name": definition.name,
                            "definition_version": definition.version,
                        },
                    )
                )
        return saved

    def set_provider_enabled(self, provider_id, enabled: bool) -> None:
        """Enable/disable a provider without deleting its durable identity."""
        from ..providers.models import ProviderStatus

        before = self.provider_store.get_provider(provider_id)
        self.provider_store.update_provider(
            provider_id,
            status=ProviderStatus.ACTIVE if enabled else ProviderStatus.DISABLED,
        )
        if enabled and before is not None:
            for binding in self.provider_store.all_bindings():
                if binding.provider_id != before.id or not binding.enabled:
                    continue
                self.event_store.append(
                    Event(
                        type="provider_available",
                        source="runtime.providers",
                        payload={
                            "provider_id": str(before.id),
                            "definition_name": binding.process_definition_name,
                            "definition_version": binding.process_definition_version,
                        },
                    )
                )

    def register_capability(self, capability, **kwargs):
        """Declare a capability without attaching it to a process yet."""
        return self.capabilities.register_capability(capability, **kwargs)

    def set_capability_enabled(self, name: str, version: str, enabled: bool) -> bool:
        """Enable or disable a capability for *future* matching (spec §43).

        Enabling counts as a new availability, so blocked work is reconsidered;
        disabling leaves running processes alone.
        """
        changed = self.capabilities.set_enabled(name, version, enabled)
        if changed and enabled:
            self.event_store.append(
                Event(
                    type="capability_available",
                    source="runtime.capabilities",
                    payload={
                        "capability_name": name,
                        "capability_version": version,
                        "reason": "enabled",
                    },
                )
            )
        return changed

    def set_project_orchestrator(self, orchestrator) -> None:
        """Give Processes a Project Orchestrator to hand incoming requests to.

        In-memory like every other registration: the orchestrator's Projects
        are durable, but which object serves them is application wiring.
        """
        self.project_orchestrator = orchestrator
        self.executor.project_orchestrator = orchestrator

    def register_pursuit_source(self, name: str, source) -> None:
        """Let a domain say what is currently being pursued.

        ``source()`` returns the identifiers of whatever that domain considers
        active — Goals today, Projects once the Control Plane is gone.  The
        Runtime does not interpret them; it only asks.  Registering the same
        name twice replaces the earlier source.
        """
        self.pursuit_sources[:] = [
            (existing, fn) for existing, fn in self.pursuit_sources if existing != name
        ]
        self.pursuit_sources.append((name, source))
        logger.info("registered pursuit source %s", name)

    def active_pursuits(self) -> list:
        """Return the identifiers of everything actively being pursued.

        Empty when nothing registered a source, which is what "this deployment
        pursues nothing on its own" should look like.
        """
        found: list = []
        for name, source in self.pursuit_sources:
            try:
                found.extend(source() or [])
            except Exception:  # noqa: BLE001 - one bad source must not blind the rest
                logger.exception("pursuit source %s failed", name)
        return found

    def register_result_applier(self, name: str, applier) -> None:
        """Let a domain commit its own records inside every activation.

        ``applier(result)`` is called inside the activation's transaction, so
        what it writes lands or rolls back with everything else.  The Runtime
        stays mechanism: it never inspects the records.  Registering the same
        name twice replaces the earlier applier, so a re-bootstrapped runtime
        does not apply the same effects twice.
        """
        self.result_appliers[:] = [
            (existing, fn) for existing, fn in self.result_appliers if existing != name
        ]
        self.result_appliers.append((name, applier))
        logger.info("registered result applier %s", name)

    def register_backend(self, name: str, backend: ExecutionBackend) -> None:
        """Register an execution backend under ``name`` (e.g. ``"llm"``).

        Handlers reach it via ``ctx.backends[name]``.  Registering a different
        implementation under the same name swaps the backend transparently.
        """
        self.backends[name] = backend
        logger.info("registered backend %s (%s)", name, type(backend).__name__)

    def register_adapter(self, adapter, *, bind: bool = True):
        """Register an external adapter under its own ``adapter_id``.

        Observer processes reach it via ``ctx.adapters``.  ``bind`` attaches the
        runtime's ingress service to adapters that keep checkpoints.
        """
        if bind and hasattr(adapter, "bind"):
            adapter.bind(self.ingress)
        return self.adapters.register(adapter)

    def set_resource_scope(self, scope: ResourceScope) -> ResourceScope:
        """Set the path boundary resource reading is confined to (Phase 3E)."""
        self.resource_scope = scope
        self.resource_service.scope = scope
        return scope

    # --- intelligence queries ---------------------------------------------

    def get_proposal(self, proposal_id) -> InterpretationProposal | None:
        """Return a stored interpretation proposal by id."""
        return self.proposal_store.get(proposal_id)

    def get_proposals(self) -> list[InterpretationProposal]:
        """Return all stored interpretation proposals."""
        return self.proposal_store.all()

    def get_llm_invocation(self, invocation_id) -> LLMInvocation | None:
        """Return a stored LLM invocation record by id."""
        return self.llm_invocation_store.get(invocation_id)

    def get_llm_invocations(self, instance_id) -> list[LLMInvocation]:
        """Return every backend call an instance made (successes and failures)."""
        return self.llm_invocation_store.for_instance(instance_id)

    # --- action queries ----------------------------------------------------

    def get_action_proposal(self, proposal_id) -> ActionProposal | None:
        """Return a stored action proposal by id."""
        return self.action_proposal_store.get(proposal_id)

    def get_action_proposals(
        self, status: ActionProposalStatus | str | None = None
    ) -> list[ActionProposal]:
        """Return all action proposals, optionally filtered by ``status``."""
        if status is None:
            return self.action_proposal_store.all()
        return self.action_proposal_store.by_status(status)

    def get_action_executions(self, proposal_id) -> list[ActionExecution]:
        """Return every execution attempt made for a proposal, oldest first."""
        return self.action_execution_store.for_proposal(proposal_id)

    def get_action_decisions(self, proposal_id) -> list[ActionDecisionRecord]:
        """Return the authorization decisions recorded for a proposal."""
        return self.action_decision_store.for_proposal(proposal_id)

    def get_action_trace(self, proposal_id) -> ActionTrace | None:
        """Trace an action back through work, delta and observation to its event."""
        return get_action_trace(
            proposal_id,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
            action_decision_store=self.action_decision_store,
            process_store=self.process_store,
            work_requirement_store=self.work_requirement_store,
            state_delta_store=self.state_delta_store,
            observation_store=self.observation_store,
            event_store=self.event_store,
            context_snapshot_store=self.context_snapshot_store,
        )

    # --- capability queries ------------------------------------------------

    def list_capabilities(self, *, enabled_only: bool = False) -> list[Capability]:
        """Return every capability the system knows about."""
        return self.capabilities.list_capabilities(enabled_only=enabled_only)

    def get_capability(self, name: str, version: str | None = None) -> Capability | None:
        """Return one capability (preferred version when unspecified)."""
        return self.capabilities.get_capability(name, version)

    def get_process_capabilities(self, name: str, version: str) -> list[Capability]:
        """Return what a ProcessDefinition declares it can accomplish."""
        return self.capabilities.get_capabilities_for_process(name, version)

    def get_definition(self, name: str, version: str) -> ProcessDefinition | None:
        """Return one persisted semantic ProcessDefinition."""
        return self.process_store.get_definition(name, version)

    def find_capable_processes(self, required) -> list:
        """Return the candidates that could do work needing ``required``."""
        requirements = [CapabilityRequirement.coerce(r) for r in required]
        definitions = self.process_store.all_definitions()
        return self.capability_matcher.match(requirements, definitions).candidates

    def match_capabilities(self, required):
        """Run the matcher and return the full :class:`MatchResult`."""
        requirements = [CapabilityRequirement.coerce(r) for r in required]
        return self.capability_matcher.match(requirements, self.process_store.all_definitions())

    def get_blocked_capability_work(self) -> list:
        """Return work the system currently lacks the competence to do."""
        return self.work_requirement_store.by_status(WorkStatus.BLOCKED_CAPABILITY)

    def get_capability_matches(self, work_requirement_id) -> list[CapabilityWorkMatch]:
        """Return every matching attempt made for a work requirement."""
        return self.capability_store.matches_for(work_requirement_id)

    def get_capability_trace(self, work_requirement_id) -> CapabilityTrace | None:
        """Trace how a unit of work was matched to a process (or not)."""
        return get_capability_trace(
            work_requirement_id,
            work_requirement_store=self.work_requirement_store,
            capability_store=self.capability_store,
            process_store=self.process_store,
        )

    # --- plan queries ------------------------------------------------------

    def get_plan(self, plan_id) -> ProcessPlan | None:
        """Return a composed plan by id."""
        return self.plan_store.get(plan_id)

    def get_plans(self, status: PlanStatus | str | None = None) -> list[ProcessPlan]:
        """Return plans, optionally filtered by status."""
        if status is None:
            return self.plan_store.all()
        return self.plan_store.by_status(status)

    def get_plans_for_work(self, work_requirement_id) -> list[ProcessPlan]:
        """Return every plan attempted for a requirement, oldest first."""
        return self.plan_store.for_work(work_requirement_id)

    def get_active_plan(self, work_requirement_id) -> ProcessPlan | None:
        """Return the plan currently pursuing a requirement."""
        return self.plan_store.active_for_work(work_requirement_id)

    def get_plan_nodes(self, plan_id) -> list[PlanNode]:
        """Return a plan's positions, in execution order."""
        return self.plan_store.nodes(plan_id)

    def get_plan_trace(self, plan_id) -> PlanTrace | None:
        """Trace a plan's structure, progress and provenance."""
        return get_plan_trace(
            plan_id,
            plan_store=self.plan_store,
            process_store=self.process_store,
            work_requirement_store=self.work_requirement_store,
            capability_store=self.capability_store,
        )

    # --- decision queries (Phase 4C) ---------------------------------------

    def get_plan_candidates(self, work_requirement_id) -> list[ProcessPlan]:
        """Return the plans for a need that could still be chosen."""
        return self.plan_store.candidates_for_work(work_requirement_id)

    def get_plan_evaluation(self, plan_id):
        """Return what one plan was estimated to cost, take, risk and yield."""
        return self.decision_store.get_evaluation(plan_id)

    def get_plan_evaluations(self, work_requirement_id) -> list:
        """Return every candidate evaluation recorded for a need."""
        return self.decision_store.evaluations_for_work(work_requirement_id)

    def get_selection_proposals(self, work_requirement_id) -> list:
        """Return every LLM suggestion made about which plan to run."""
        return self.decision_store.proposals_for_work(work_requirement_id)

    def get_selection_proposal(self, proposal_id):
        """Return one selection proposal by id."""
        return self.decision_store.get_proposal(proposal_id)

    def get_plan_selections(self, work_requirement_id) -> list:
        """Return every decision made about this need, oldest first.

        A list, not a value: replanning makes a second decision, and both are
        kept (Invariant 78).
        """
        return self.decision_store.selections_for_work(work_requirement_id)

    def get_replan_attempts(self, work_requirement_id) -> list:
        """Return every attempt to find another way after a plan failed."""
        return self.decision_store.replan_attempts_for_work(work_requirement_id)

    def get_decision_trace(self, work_requirement_id) -> DecisionTrace | None:
        """Trace every decision made about how to satisfy one need (spec §68)."""
        return get_decision_trace(
            work_requirement_id,
            work_requirement_store=self.work_requirement_store,
            plan_store=self.plan_store,
            decision_store=self.decision_store,
            llm_invocation_store=self.llm_invocation_store,
        )

    def set_llm_plan_selector(self, selector) -> None:
        """Install (or remove, with ``None``) the optional LLM plan selector.

        Optional in the strong sense (Invariant 77): with none installed the
        deterministic selector decides and the system runs unchanged.
        """
        self.llm_plan_selector = selector

    # --- self-extension queries (Phase 5A) ---------------------------------

    def get_capability_gap(self, gap_id) -> CapabilityGap | None:
        """Return one recorded deficiency by id."""
        return self.extension_store.get_gap(gap_id)

    def get_capability_gaps(
        self, status: CapabilityGapStatus | str | None = None
    ) -> list[CapabilityGap]:
        """Return every gap, optionally filtered by status."""
        if status is None:
            return self.extension_store.all_gaps()
        return self.extension_store.gaps_by_status(status)

    def get_open_capability_gaps(self) -> list[CapabilityGap]:
        """Return the deficiencies that still stand (spec §133)."""
        return self.extension_store.open_gaps()

    def get_capability_gaps_for_work(self, work_requirement_id) -> list[CapabilityGap]:
        """Return every gap ever opened for one need."""
        return self.extension_store.gaps_for_work(work_requirement_id)

    def get_extension_proposal(self, proposal_id) -> ExtensionProposal | None:
        """Return one extension proposal by id."""
        return self.extension_store.get_proposal(proposal_id)

    def get_extension_proposals(
        self, status: ExtensionProposalStatus | str | None = None
    ) -> list[ExtensionProposal]:
        """Return extension proposals, optionally filtered by status."""
        if status is None:
            return self.extension_store.all_proposals()
        return self.extension_store.proposals_by_status(status)

    def get_extension_proposals_for_gap(self, gap_id) -> list[ExtensionProposal]:
        """Return every proposal made about a gap, oldest first."""
        return self.extension_store.proposals_for_gap(gap_id)

    def get_extension_decisions(self, proposal_id) -> list[ExtensionDecisionRecord]:
        """Return the decisions recorded for a proposal (append-only)."""
        return self.extension_store.decisions_for_proposal(proposal_id)

    def get_capability_gap_trace(self, gap_id) -> CapabilityGapTrace | None:
        """Trace a deficiency back to the need and the match that found it."""
        return get_capability_gap_trace(
            gap_id,
            extension_store=self.extension_store,
            work_requirement_store=self.work_requirement_store,
            capability_store=self.capability_store,
        )

    def get_extension_trace(self, proposal_id) -> ExtensionTrace | None:
        """Trace a proposed self-extension back to the event that caused it."""
        return get_extension_trace(
            proposal_id,
            extension_store=self.extension_store,
            work_requirement_store=self.work_requirement_store,
            capability_store=self.capability_store,
            state_delta_store=self.state_delta_store,
            observation_store=self.observation_store,
            event_store=self.event_store,
            llm_invocation_store=self.llm_invocation_store,
            context_snapshot_store=self.context_snapshot_store,
        )

    def get_extension_health(self) -> dict:
        """A small operational snapshot of the self-extension layer (spec §134).

        ``approved_not_constructed`` now distinguishes approvals still waiting
        for Phase 5B from plans already built or terminally checked.
        """
        gaps = self.extension_store.all_gaps()
        open_gaps = [g for g in gaps if not g.status.terminal]
        proposals = self.extension_store.all_proposals()
        by_status: dict[str, int] = {}
        for proposal in proposals:
            by_status[proposal.status.value] = by_status.get(proposal.status.value, 0) + 1
        oldest = min((g.created_at for g in open_gaps), default=None)
        construction_plans = self.construction_store.all_plans()
        installation_plans = self.installation_store.all_plans()
        planned_proposals = {p.extension_proposal_id for p in construction_plans}
        return {
            "open_gaps": len(open_gaps),
            "resolved_gaps": sum(
                1 for g in gaps if g.status is CapabilityGapStatus.RESOLVED
            ),
            "proposals": len(proposals),
            "proposals_by_status": by_status,
            "awaiting_review": by_status.get(ExtensionProposalStatus.REVIEW.value, 0),
            "approved_not_constructed": sum(
                1 for p in proposals
                if p.status is ExtensionProposalStatus.APPROVED
                and p.id not in planned_proposals
            ),
            "construction_verified": sum(
                1 for p in construction_plans if p.status.value == "VERIFIED"
            ),
            "construction_failed_or_blocked": sum(
                1 for p in construction_plans if p.status.value in {"FAILED", "BLOCKED"}
            ),
            "rejected": by_status.get(ExtensionProposalStatus.REJECTED.value, 0)
            + by_status.get(ExtensionProposalStatus.INVALID.value, 0),
            "critical_proposals": sum(
                1 for p in proposals if p.estimated_risk.value == "CRITICAL"
            ),
            "oldest_open_gap_at": oldest.isoformat() if oldest else None,
            "installation_awaiting_review": sum(
                1 for p in installation_plans if p.status.value == "REVIEW"
            ),
            "installation_rolled_back": sum(
                1 for p in installation_plans if p.status.value == "ROLLED_BACK"
            ),
            "capabilities_acquired": len(self.installation_store.active_activations()),
        }

    def set_llm_extension_proposer(self, proposer) -> None:
        """Install (or remove, with ``None``) the optional LLM proposer.

        Optional in the strong sense (spec §81–§82): with none installed the
        deterministic builder writes the proposal and the whole loop still runs.
        """
        self.llm_extension_proposer = proposer

    def set_extension_policy(self, policy: ExtensionPolicy) -> ExtensionPolicy:
        """Set the risk appetite used when a runtime-level default is wanted.

        The analyzing process still reads its *own* definition metadata
        (spec §42) — this is the default a bootstrap copies onto it.
        """
        self.extension_policy = policy
        return policy

    # --- sandboxed construction queries (Phase 5B) ------------------------

    def get_construction_plan(self, plan_id):
        """Return one durable construction plan, including its steps."""
        return self.construction_store.get_plan(plan_id)

    def get_construction_plans(self, proposal_id=None) -> list:
        """Return construction plans globally or for one ExtensionProposal."""
        if proposal_id is None:
            return self.construction_store.all_plans()
        return self.construction_store.plans_for_proposal(proposal_id)

    def get_sandbox_workspace(self, workspace_id):
        """Return one isolated construction workspace record."""
        return self.construction_store.get_workspace(workspace_id)

    def get_construction_grant(self, plan_id):
        """Return the construction-scoped grant for a plan."""
        return self.construction_store.get_grant_for_plan(plan_id)

    def get_verification_checks(self, plan_id) -> list:
        """Return a plan's structural, static and behavior checks."""
        return self.construction_store.checks_for_plan(plan_id)

    def get_construction_result(self, plan_id):
        """Return a plan's terminal VERIFIED/FAILED/BLOCKED result."""
        return self.construction_store.result_for_plan(plan_id)

    # --- production installation queries (Phase 5C) ----------------------

    def get_installation_plan(self, plan_id):
        return self.installation_store.get_plan(plan_id)

    def get_installation_plans(self) -> list:
        return self.installation_store.all_plans()

    def get_installation_grant(self, plan_id):
        return self.installation_store.grant_for_plan(plan_id)

    def get_installation_checks(self, plan_id) -> list:
        return self.installation_store.checks_for_plan(plan_id)

    def get_installation_result(self, plan_id):
        return self.installation_store.result_for_plan(plan_id)

    def get_activation_record(self, plan_id):
        return self.installation_store.activation_for_plan(plan_id)

    def get_installation_trace(self, plan_id) -> InstallationTrace | None:
        return get_installation_trace(
            plan_id,
            installation_store=self.installation_store,
            construction_store=self.construction_store,
            extension_store=self.extension_store,
            work_requirement_store=self.work_requirement_store,
            resource_store=self.resource_store,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
            capability_store=self.capability_store,
            event_store=self.event_store,
        )

    def set_installation_policy(self, policy: InstallationPolicy) -> InstallationPolicy:
        """Narrow reviewable installation strategies; never enable auto-approval."""
        self.installation_policy = policy
        return policy

    def get_construction_trace(self, plan_id) -> ConstructionTrace | None:
        """Join Work -> Gap -> approval -> sandbox -> evidence."""
        return get_construction_trace(
            plan_id,
            construction_store=self.construction_store,
            extension_store=self.extension_store,
            work_requirement_store=self.work_requirement_store,
            action_proposal_store=self.action_proposal_store,
            action_execution_store=self.action_execution_store,
            resource_store=self.resource_store,
            context_snapshot_store=self.context_snapshot_store,
            llm_invocation_store=self.llm_invocation_store,
        )

    # --- bounded autonomous acquisition queries (Phase 5D) ---------------

    def get_acquisition_session(self, session_id):
        """Return one durable acquisition session."""
        return self.autonomy_store.get_session(session_id)

    def get_acquisition_sessions(self, *, status=None) -> list:
        """List sessions, optionally filtered by lifecycle status."""
        return self.autonomy_store.sessions(status=status)

    def get_active_acquisitions(self) -> list:
        """Return every acquisition that still has a possible next step."""
        return [session for session in self.autonomy_store.sessions() if not session.status.terminal]

    def get_blocked_acquisitions(self) -> list:
        """Return policy/budget/cycle blocked acquisitions."""
        return self.autonomy_store.sessions(status=AcquisitionStatus.BLOCKED)

    def get_autonomy_decisions(self, session_id) -> list:
        return self.autonomy_store.decisions(session_id)

    def get_acquisition_attempts(self, session_id, attempt_type=None) -> list:
        return self.autonomy_store.attempts(session_id, attempt_type)

    def get_acquisition_trace(self, session_id) -> AcquisitionTrace | None:
        return get_acquisition_trace(
            session_id, autonomy_store=self.autonomy_store,
            extension_store=self.extension_store,
            construction_store=self.construction_store,
            installation_store=self.installation_store,
            work_requirement_store=self.work_requirement_store,
            event_store=self.event_store,
            installation_trace_getter=self.get_installation_trace,
        )

    def get_autonomy_health(self) -> dict:
        sessions = self.autonomy_store.sessions()
        return {
            "active": sum(not session.status.terminal for session in sessions),
            "waiting_review": sum(session.status is AcquisitionStatus.WAITING_REVIEW for session in sessions),
            "blocked": sum(session.status is AcquisitionStatus.BLOCKED for session in sessions),
            "completed": sum(session.status is AcquisitionStatus.COMPLETED for session in sessions),
            "failed": sum(session.status is AcquisitionStatus.FAILED for session in sessions),
            "cancelled": sum(session.status is AcquisitionStatus.CANCELLED for session in sessions),
        }

    def set_autonomy_policy(self, policy: AutonomyPolicy) -> AutonomyPolicy:
        """Set deployment configuration; acquisition Processes only read it."""
        self.autonomy_policy = policy
        return policy

    def set_default_autonomy_budget(self, budget: AutonomyBudget) -> AutonomyBudget:
        """Set the immutable snapshot used for newly opened sessions."""
        self.default_autonomy_budget = budget
        return budget

    def set_llm_construction_generator(self, generator) -> None:
        """Install/remove the optional generator; output remains sandbox-only."""
        self.llm_construction_generator = generator

    def acquisition_environment(self) -> AcquisitionEnvironment:
        """What the analyzer is allowed to look at, as it currently stands.

        Assembled here because the Runtime is what holds the registrations —
        but it holds no opinion about them (Invariant 4, spec §51): the
        reasoning lives in the analyzer, this is only the inventory.
        """
        return AcquisitionEnvironment(
            definitions=self.process_store.all_definitions(),
            backends={
                name: capabilities_of(backend)
                for name, backend in self.backends.items()
            },
            extractors=self.extractors,
            adapters=self.adapters,
            plugins=self.plugin_catalog,
        )

    # --- event delivery queries -------------------------------------------

    def get_event_delivery(self, event_id) -> EventDelivery | None:
        """Return an event's delivery record — status, attempts, last error."""
        return self.event_delivery_store.get(event_id)

    def get_event_deliveries(
        self, status: EventDeliveryStatus | str | None = None
    ) -> list[EventDelivery]:
        """Return delivery records, optionally filtered by ``status``."""
        if status is None:
            return self.event_delivery_store.all()
        return self.event_delivery_store.by_status(status)

    def get_pending_event_delivery_count(self) -> int:
        """How many persisted events still owe a routing attempt."""
        return self.event_delivery_store.count_pending()

    def get_failed_event_deliveries(self) -> list[EventDelivery]:
        """Return deliveries that were given up on (should normally be empty)."""
        return self.event_delivery_store.by_status(EventDeliveryStatus.FAILED)

    def get_delivery_health(self) -> dict:
        """A small operational snapshot of the delivery queue."""
        return self.dispatcher.health()

    # --- ingress queries ---------------------------------------------------

    def get_ingress_receipt(self, receipt_id) -> IngressReceipt | None:
        """Return a stored ingress receipt by id."""
        return self.ingress_receipt_store.get(receipt_id)

    def get_ingress_receipts(self, adapter_id: str | None = None) -> list[IngressReceipt]:
        """Return ingress receipts, optionally only those from one adapter."""
        if adapter_id is None:
            return self.ingress_receipt_store.all()
        return self.ingress_receipt_store.for_adapter(adapter_id)

    def get_ingress_receipt_for_event(self, event_id) -> IngressReceipt | None:
        """Return the receipt that produced ``event_id`` (``None`` if internal)."""
        return self.ingress_receipt_store.for_event(event_id)

    def get_adapter_checkpoint(self, adapter_id: str, stream_key: str) -> AdapterCheckpoint | None:
        """Return one adapter's observation position for a stream."""
        return self.adapter_checkpoint_store.get(adapter_id, stream_key)

    def get_adapter_checkpoints(self, adapter_id: str) -> list[AdapterCheckpoint]:
        """Return every stream checkpoint held by ``adapter_id``."""
        return self.adapter_checkpoint_store.for_adapter(adapter_id)

    def get_ingress_trace(self, event_id) -> IngressTrace | None:
        """Trace an ingested event forward to everything it caused."""
        return get_ingress_trace(event_id, **self._ingress_trace_stores())

    def get_ingress_trace_by_source_key(
        self, adapter_id: str, source_event_key: str
    ) -> IngressTrace | None:
        """Trace an external identity forward, without knowing any NEXUS id."""
        return get_ingress_trace_by_source_key(
            adapter_id, source_event_key, **self._ingress_trace_stores()
        )

    def _ingress_trace_stores(self) -> dict:
        return {
            "ingress_receipt_store": self.ingress_receipt_store,
            "event_store": self.event_store,
            "observation_store": self.observation_store,
            "state_delta_store": self.state_delta_store,
            "work_requirement_store": self.work_requirement_store,
            "action_proposal_store": self.action_proposal_store,
        }

    # --- resource queries --------------------------------------------------

    def get_resource(self, resource_id) -> Resource | None:
        """Return a Resource by id."""
        return self.resource_store.get_resource(resource_id)

    def get_resource_by_uri(self, uri: str) -> Resource | None:
        """Return a Resource by its URI."""
        return self.resource_store.get_resource_by_uri(uri)

    def get_resources(self) -> list[Resource]:
        """Return every Resource, oldest first."""
        return self.resource_store.all_resources()

    def get_current_resource_version(self, resource_id) -> ResourceVersion | None:
        """Return the newest version of a Resource."""
        return self.resource_store.get_current_version(resource_id)

    def get_resource_versions(self, resource_id) -> list[ResourceVersion]:
        """Return every version of a Resource, oldest first."""
        return self.resource_store.get_versions(resource_id)

    def get_resource_version(self, resource_id, version: int) -> ResourceVersion | None:
        """Return a specific numbered version of a Resource."""
        return self.resource_store.get_version_number(resource_id, version)

    def get_representation(self, representation_id) -> ResourceRepresentation | None:
        """Return a Representation by id."""
        return self.resource_store.get_representation(representation_id)

    def get_representations(self, resource_version_id) -> list[ResourceRepresentation]:
        """Return every Representation of a ResourceVersion."""
        return self.resource_store.list_representations(resource_version_id)

    def find_representation(
        self, resource_version_id, representation_type: str
    ) -> ResourceRepresentation | None:
        """Return one Representation of a version by type."""
        return self.resource_store.find_representation(
            resource_version_id, representation_type
        )

    def get_resource_trace(self, resource_version_id) -> ResourceTrace | None:
        """Trace a ResourceVersion back to the external source it came from."""
        return get_resource_trace(
            resource_version_id,
            resource_store=self.resource_store,
            event_store=self.event_store,
            ingress_receipt_store=self.ingress_receipt_store,
        )

    def get_representation_trace(self, representation_id) -> RepresentationTrace | None:
        """Trace a Representation back through its extractor to the world."""
        return get_representation_trace(
            representation_id,
            resource_store=self.resource_store,
            process_store=self.process_store,
            event_store=self.event_store,
            ingress_receipt_store=self.ingress_receipt_store,
        )

    # --- world-state queries ----------------------------------------------

    def get_current_state(self, entity: str, attribute: str) -> StateEntry | None:
        """Return the current fact for ``entity.attribute`` (with provenance ids)."""
        return self.state_store.get_current(entity, attribute)

    def get_state_history(
        self, entity: str, attribute: str
    ) -> list[StateHistoryEntry]:
        """Return every version of ``entity.attribute``, oldest first."""
        return self.state_store.get_history(entity, attribute)

    def get_state_at_version(
        self, entity: str, attribute: str, version: int
    ) -> StateHistoryEntry | None:
        """Return a specific historical version of ``entity.attribute``."""
        return self.state_store.get_state_at_version(entity, attribute, version)

    def get_state_provenance(self, entity: str, attribute: str) -> Provenance | None:
        """Walk ``entity.attribute`` back to the raw event that produced it."""
        return get_state_provenance(
            entity,
            attribute,
            state_store=self.state_store,
            state_delta_store=self.state_delta_store,
            observation_store=self.observation_store,
            event_store=self.event_store,
        )

    def rebuild_current_state(self) -> int:
        """Rebuild the ``current`` projection from history; return facts rebuilt."""
        return self.state_store.rebuild_current_state()

    # --- work-intelligence queries ----------------------------------------

    def get_work_requirement(self, requirement_id) -> WorkRequirement | None:
        """Return a work requirement by id."""
        return self.work_requirement_store.get(requirement_id)

    def get_work_requirements(
        self, status: WorkStatus | str | None = None
    ) -> list[WorkRequirement]:
        """Return all work requirements, optionally filtered by ``status``."""
        if status is None:
            return self.work_requirement_store.all()
        return self.work_requirement_store.by_status(status)

    def get_work_trace(self, requirement_id) -> WorkTrace | None:
        """Trace a work requirement back to the raw event that caused it."""
        return get_work_trace(
            requirement_id,
            work_requirement_store=self.work_requirement_store,
            process_store=self.process_store,
            state_delta_store=self.state_delta_store,
            observation_store=self.observation_store,
            event_store=self.event_store,
        )

    # --- context (audit) ---------------------------------------------------

    def get_context_snapshots(self, instance_id) -> list[ContextSnapshot]:
        """Return all compiled-context audit snapshots for an instance."""
        return self.context_snapshot_store.for_instance(instance_id)

    def get_latest_context_snapshot(self, instance_id) -> ContextSnapshot | None:
        """Return the most recent context snapshot for an instance."""
        return self.context_snapshot_store.latest_for_instance(instance_id)

    # --- event intake ------------------------------------------------------

    async def submit_event(
        self, event: Event, budget: DrainBudget | None = None
    ) -> list[Event]:
        """Ingest an external event and run the system until it goes idle.

        Idempotent: an event whose id was already ingested is ignored.  Returns
        every event produced by processes while draining (the submitted event
        itself is not included).
        """
        if self.event_store.get(event.id) is not None:
            logger.info("event %s already ingested; skipping (idempotent)", event.id)
            return []
        # Appending records the delivery obligation in the same transaction, so
        # from here on the event cannot be forgotten even if we crash now.
        self.event_store.append(event)
        return await self._drain(budget)

    async def drain(self, budget: DrainBudget | None = None) -> list[Event]:
        """Run one slice of pending work; see :class:`DrainBudget`.

        The explicit way to drive the runtime in short steps, so an outer loop
        stays responsive without anything being lost between slices.
        """
        return await self._drain(budget)

    async def deliver_event(self, event: Event) -> list[Event]:
        """Run the system after an event was persisted elsewhere.

        Since Phase 3F this is an **optimization, not the durability
        mechanism** (Invariant 46).  The event's delivery obligation was
        recorded when it was appended; this just wakes the loop so it happens
        now rather than at the next sweep.  Not calling it loses nothing.
        """
        return await self._drain()

    def dispatch_pending_events(self, limit: int | None = None) -> int:
        """Route every event that is persisted but not yet acknowledged.

        The durable half of the loop: safe to call at any time, from a restart,
        a tick, or by hand.
        """
        return self.dispatcher.dispatch_pending(limit)

    async def run_pending(self, budget: DrainBudget | None = None) -> list[Event]:
        """Drain any already-RUNNABLE processes (e.g. after :meth:`recover`)."""
        return await self._drain(budget)

    async def tick(self, budget: DrainBudget | None = None) -> list[Event]:
        """Fire due timers, promote due retries, then drain.

        Time comes from the runtime's :class:`Clock`, so tests can drive this
        deterministically with a :class:`~nexus_seed.runtime.clock.ManualClock`.
        """
        now = self.clock.now()
        fired_events: list[Event] = []
        for timer in self.timer_store.due(now):
            self.timer_store.mark_fired(timer.id)
            fired = Event(
                type=timer.event_type, source="runtime.timer", payload=timer.payload
            )
            # Appending records the delivery obligation; the drain below routes
            # it.  A crash in between leaves the timer event pending, not lost.
            self.event_store.append(fired)
            fired_events.append(fired)
            logger.info("timer %s fired -> %s", timer.id, timer.event_type)

        for instance in self.process_store.due_retries(now.isoformat()):
            instance.status = ProcessStatus.RUNNABLE
            instance.next_retry_at = None
            instance.updated_at = now
            self.process_store.save_instance(instance)
            logger.info("retry due: instance %s -> RUNNABLE", instance.id)

        return fired_events + await self._drain(budget)

    # --- internals ---------------------------------------------------------

    async def _drain(self, budget: DrainBudget | None = None) -> list[Event]:
        """Deliver outstanding events and run RUNNABLE processes for one slice.

        Two alternating halves.  Delivery turns persisted events into
        activations; execution turns activations into more persisted events.
        Without a budget the loop ends when neither has anything left to do —
        the behaviour every earlier phase relied on.

        With a budget it may also stop *mid-flight*, which is not a failure
        (Invariant 71): everything is durable, so the next tick continues
        (Invariant 73).  The alternation is what keeps either half from eating
        the whole budget on its own (spec §47).

        Note what the executor does *not* do any more: it never routes the
        events a process emitted.  Those were appended with delivery
        obligations, and the dispatcher picks them up on the next pass — so a
        crash between "the process committed" and "its events were routed" is
        no longer a way to lose them (Invariant 44).
        """
        budget = budget or self.default_drain_budget
        result = DrainResult()

        while True:
            if budget.exhausted_by(
                dispatches=result.dispatches,
                activations=result.activations,
                cycles=result.cycles,
            ):
                result.exhausted = True
                break
            result.cycles += 1

            allowance = budget.dispatch_allowance(result.dispatches)
            delivered = 0
            if allowance is None or allowance > 0:
                delivered = self.dispatcher.dispatch_pending(allowance)
                result.dispatches += delivered

            instance = self.scheduler.next_runnable()
            if instance is None:
                if delivered == 0:
                    break
                continue

            activation = await self.executor.execute(instance)
            result.activations += 1
            result.produced_events.extend(activation.emitted_events)

            # instance.status was updated in place by the executor.
            for join_event in self.join_coordinator.on_instance_finished(instance):
                self.event_store.append(join_event)
                result.produced_events.append(join_event)

        result.remaining_deliveries = self.event_delivery_store.count_pending()
        result.remaining_runnable_processes = len(
            self.process_store.instances_by_status(ProcessStatus.RUNNABLE)
        )
        self.last_drain = result
        if result.exhausted:
            logger.info(
                "drain yielded at budget: %d dispatch(es), %d activation(s), "
                "%d delivery/deliveries and %d process(es) remaining",
                result.dispatches,
                result.activations,
                result.remaining_deliveries,
                result.remaining_runnable_processes,
            )
        return result.produced_events

    def recover(self) -> list[ProcessInstance]:
        """Reconcile everything an interrupted run may have left behind.

        Three sweeps, all resting on the same argument: a committed transaction
        always advances its marker, so a marker still in the "in progress"
        state was never committed and is safe to redo.

        * legacy events (no delivery record at all) are backfilled as already
          DELIVERED — see :meth:`DurableEventDispatcher.backfill_legacy`;
        * interrupted DELIVERING deliveries return to PENDING;
        * crash-orphaned RUNNING instances return to RUNNABLE.
        """
        self.dispatcher.backfill_legacy()
        self.dispatcher.recover()

        recovered = []
        for instance in self.process_store.instances_by_status(ProcessStatus.RUNNING):
            instance.status = ProcessStatus.RUNNABLE
            instance.updated_at = self.clock.now()
            self.process_store.save_instance(instance)
            logger.warning("recovered instance %s: RUNNING -> RUNNABLE", instance.id)
            recovered.append(instance)
        return recovered

    # --- lifecycle ---------------------------------------------------------

    # --- provider federation ---------------------------------------------

    def get_execution_providers(self):
        """Return all registered execution providers, including disabled ones."""
        return self.provider_store.all_providers()

    def get_provider_bindings(self, name: str | None = None, version: str | None = None):
        """Return provider bindings globally or for one ProcessDefinition."""
        if name is None:
            return self.provider_store.all_bindings()
        if version is None:
            return [
                binding
                for binding in self.provider_store.all_bindings()
                if binding.process_definition_name == name
            ]
        return self.provider_store.bindings_for(name, version)

    def get_provider_invocations(self, *, provider_id=None, status=None):
        """Return durable external delegation attempts."""
        return self.provider_store.invocations(provider_id=provider_id, status=status)

    def get_provider_selections(self, process_instance_id):
        """Return the deterministic provider-selection audit for an instance."""
        return self.provider_store.selections_for_instance(process_instance_id)

    def get_imported_skills(self):
        """Return directory skills imported through the safety pipeline."""
        return self.provider_store.imported_skills()

    def get_provider_health(self, provider_id=None) -> dict:
        """Return provider and invocation health counters for operations."""
        if provider_id is not None:
            provider = self.provider_store.get_provider(provider_id)
            if provider is None:
                return {}
            invocations = self.provider_store.invocations(provider_id=provider_id)
            return {
                "provider": provider,
                "operational": self.providers._provider_available(provider),
                "invocations": len(invocations),
                "failed_invocations": sum(
                    inv.status.value == "FAILED" for inv in invocations
                ),
            }
        providers = self.provider_store.all_providers()
        invocations = self.provider_store.invocations()
        external_ids = {
            provider.id
            for provider in providers
            if provider.kind.value != "INTERNAL"
        }
        selections = self.provider_store.all_selections()
        selection_counts: dict[str, int] = {}
        for selection in selections:
            if selection.provider_id is None:
                continue
            key = str(selection.provider_id)
            selection_counts[key] = selection_counts.get(key, 0) + 1
        completed_latencies = [
            (inv.completed_at - inv.started_at).total_seconds()
            for inv in invocations
            if inv.completed_at is not None
        ]
        return {
            "providers": len(providers),
            "operational": sum(self.providers._provider_available(p) for p in providers),
            "active_providers": sum(p.status.value == "ACTIVE" for p in providers),
            "unavailable_providers": sum(
                p.status.value == "UNAVAILABLE" or p.health.value == "UNAVAILABLE"
                for p in providers
            ),
            "by_health": self.providers.provider_health(),
            "invocations": len(invocations),
            "waiting_external": sum(
                inv.status.value == "WAITING_EXTERNAL" for inv in invocations
            ),
            "external_invocations_running": sum(
                inv.provider_id in external_ids
                and inv.status.value in {"PENDING", "RUNNING", "WAITING_EXTERNAL"}
                for inv in invocations
            ),
            "failed_invocations": sum(
                inv.status.value == "FAILED" for inv in invocations
            ),
            "average_latency_seconds": (
                sum(completed_latencies) / len(completed_latencies)
                if completed_latencies
                else None
            ),
            "provider_selection_counts": selection_counts,
        }

    def get_provider_trace(self, invocation_id) -> ProviderTrace | None:
        """Join delegation provenance through process, work, plan and Context."""
        return get_provider_trace(
            invocation_id,
            provider_store=self.provider_store,
            process_store=self.process_store,
            work_store=self.work_requirement_store,
            plan_store=self.plan_store,
            context_store=self.context_snapshot_store,
        )

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
