"""One Work, three external Skills, in the order the data flows.

The analysis chain is the case composition exists for: no single competence
turns a CSV into ranked causal factors, but three of them in order do.  What
this file proves is that the chain still works when every step lives behind an
A2A provider — each intermediate result leaves the system as a typed output and
comes back as the next step's typed input.

The Skill packages under test are the ones this repository ships, not fixtures,
so a change to their declared ports fails here rather than in production.
"""

from __future__ import annotations

import json
from pathlib import Path

from nexus_seed.federation_config import FederationSettings, configure_external_agents
from nexus_seed.planning.models import PlanStatus
from nexus_seed.providers import ProviderInvocationStatus
from nexus_seed.runtime.runtime import Runtime
from nexus_seed.work.work_requirement import WorkStatus
from planning_helpers import make_work, offer_work, only_plan, status_of, wire
from tests.a2a_helpers import FakeA2AServer, data_artifact, task

REPO = Path(__file__).resolve().parent.parent

#: The chain as the shipped skill packages declare it: skill, inputs, outputs.
CHAIN = (
    ("load_and_clean_csv", ("csv_file",), ("cleaned_dataframe",)),
    ("compute_sales_metrics", ("cleaned_dataframe",), ("sales_trend_table",)),
    ("perform_causal_analysis", ("sales_trend_table",), ("causal_factors_list",)),
)

#: The first heading of each SKILL.md, which is how the fake worker tells the
#: three requests apart — one agent runs all three steps.
TITLES = {
    "load_and_clean_csv": "Load and Clean CSV",
    "compute_sales_metrics": "Compute Sales Metrics",
    "perform_causal_analysis": "Perform Causal Analysis",
}

#: What each step answers with, shaped like its declared output schema.
ANSWERS = {
    "load_and_clean_csv": {
        "path": "data/sales_july_clean.csv",
        "row_count": 1180,
        "columns": ["date", "category", "region", "revenue", "units"],
        "dropped_rows": 20,
    },
    "compute_sales_metrics": {
        "path": "data/sales_july_trend.csv",
        "period": "2026-07",
        "baseline_period": "2026-06",
        "metrics": [{"name": "revenue", "value": 812000, "change_percent": -14.2}],
    },
    "perform_causal_analysis": {
        "summary": "Two categories account for most of the decline.",
        "factors": [
            {
                "factor": "Outdoor category",
                "evidence": ["revenue -31% vs June"],
                "confidence": 0.78,
            }
        ],
        "unexplained_share": 0.19,
    },
}


def analysis_settings(url):
    """Bind the three analysis capabilities to one external worker."""
    return FederationSettings.from_dict({
        "providers": {
            "analysis_worker": {"type": "a2a", "url": url, "poll_interval_seconds": 0.01}
        },
        "bindings": {name: {"provider": "analysis_worker"} for name, _, _ in CHAIN},
        "skills": {"roots": [str(REPO / "skills")]},
    })


class AnalysisWorker(FakeA2AServer):
    """One agent running all three steps, answering according to what was asked.

    The reply depends on the request rather than on call order, which is also
    what lets a test read back what each step was actually handed.
    """

    def __init__(self):
        super().__init__({})
        self._server.next_response = self._answer

    def _stage(self, params) -> str | None:
        heading = self._heading(params)
        return next((s for s, title in TITLES.items() if title == heading), None)

    @staticmethod
    def _heading(params) -> str:
        data = params["message"]["parts"][0]["data"]
        return data["instruction"].splitlines()[0].lstrip("#").strip()

    def _answer(self, method: str):
        if method != "message/send":
            return None
        stage = self._stage(self.calls("message/send")[-1]["params"])
        if stage is None:
            return None
        return task(
            "completed", task_id=f"task-{stage}", artifacts=[data_artifact(ANSWERS[stage])]
        )

    def handled(self) -> dict:
        """What each step was handed, keyed by the Skill that ran, in order."""
        out = {}
        for call in self.calls("message/send"):
            stage = self._stage(call["params"])
            data = call["params"]["message"]["parts"][0]["data"]
            out[stage] = data["context"]["typed_inputs"]
        return out


def analysis_work(runtime):
    """One Work needing the whole chain, as decomposition should now state it."""
    return make_work(
        runtime,
        work_key="analysis:sales-july",
        work_type="sales_decline_analysis",
        required=tuple(name for name, _, _ in CHAIN),
        inputs=("csv_file",),
        outputs=("causal_factors_list",),
    )


async def test_the_three_skills_compose_into_one_plan(tmp_path):
    with AnalysisWorker() as agent:
        runtime = wire(Runtime(tmp_path / "chain.db"))
        report = configure_external_agents(runtime, settings=analysis_settings(agent.url))

        await offer_work(runtime, analysis_work(runtime))

        assert [name for name, _, _ in CHAIN] not in report.unroutable
        plan = only_plan(runtime)
        assert plan.status is PlanStatus.COMPLETED
        assert [n.node_key for n in runtime.get_plan_nodes(plan.id)] == [
            "load_and_clean_csv:v1",
            "compute_sales_metrics:v1",
            "perform_causal_analysis:v1",
        ]
        runtime.close()


async def test_each_step_receives_what_the_previous_one_produced(tmp_path):
    """The intermediate result crosses the A2A boundary and comes back typed."""
    with AnalysisWorker() as agent:
        runtime = wire(Runtime(tmp_path / "flow.db"))
        configure_external_agents(runtime, settings=analysis_settings(agent.url))

        await offer_work(runtime, analysis_work(runtime))

        handled = agent.handled()
        intermediates = {"cleaned_dataframe", "sales_trend_table", "causal_factors_list"}
        # The first step starts the chain, so nothing upstream is handed to it.
        # It still carries the plan bookkeeping every delegation carries.
        assert not intermediates & set(handled["load_and_clean_csv"])
        assert handled["compute_sales_metrics"]["cleaned_dataframe"] == (
            ANSWERS["load_and_clean_csv"]
        )
        assert handled["perform_causal_analysis"]["sales_trend_table"] == (
            ANSWERS["compute_sales_metrics"]
        )
        # Each step is handed its own input and not the whole history.
        assert "cleaned_dataframe" not in handled["perform_causal_analysis"]
        runtime.close()


async def test_the_steps_run_in_data_flow_order(tmp_path):
    with AnalysisWorker() as agent:
        runtime = wire(Runtime(tmp_path / "order.db"))
        configure_external_agents(runtime, settings=analysis_settings(agent.url))

        await offer_work(runtime, analysis_work(runtime))

        assert list(agent.handled()) == [name for name, _, _ in CHAIN]
        runtime.close()


async def test_the_work_is_satisfied_by_the_declared_output(tmp_path):
    with AnalysisWorker() as agent:
        runtime = wire(Runtime(tmp_path / "satisfied.db"))
        configure_external_agents(runtime, settings=analysis_settings(agent.url))
        work = analysis_work(runtime)

        await offer_work(runtime, work)

        assert status_of(runtime, work) is WorkStatus.SATISFIED
        invocations = runtime.provider_store.invocations()
        assert len(invocations) == 3
        assert all(i.status is ProviderInvocationStatus.COMPLETED for i in invocations)
        runtime.close()


async def test_a_broken_middle_step_stops_the_chain(tmp_path):
    """The third step must not run on data the second one never produced."""

    class Broken(AnalysisWorker):
        def _answer(self, method):
            if method == "message/send":
                stage = self._stage(self.calls("message/send")[-1]["params"])
                if stage == "compute_sales_metrics":
                    return task("failed", task_id="task-broken")
            return super()._answer(method)

    with Broken() as agent:
        runtime = wire(Runtime(tmp_path / "broken.db"))
        configure_external_agents(runtime, settings=analysis_settings(agent.url))
        work = analysis_work(runtime)

        await offer_work(runtime, work)

        assert "perform_causal_analysis" not in agent.handled()
        assert status_of(runtime, work) is not WorkStatus.SATISFIED
        runtime.close()


def test_the_shipped_skill_packages_declare_a_connectable_chain():
    """The ports in skills/*/skill.json are what make the chain composable."""
    declared = {}
    for name, _, _ in CHAIN:
        manifest = json.loads(
            (REPO / "skills" / name / "skill.json").read_text(encoding="utf-8")
        )
        declared[name] = (tuple(manifest["input_ports"]), tuple(manifest["output_ports"]))

    assert declared == {name: (i, o) for name, i, o in CHAIN}
    # Each intermediate type has exactly one producer, so no consumer has to
    # guess which upstream step fed it.
    produced = [t for _, outputs in declared.values() for t in outputs]
    assert len(produced) == len(set(produced))
