"""Structured verification runner; deliberately not a general shell executor."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..resources.scope import ResourceScope, ScopeViolation
from .models import CapabilityAssertion, CapabilityContract


class TestRunType(str, Enum):
    PYTHON_SYNTAX = "PYTHON_SYNTAX"
    PYTHON_IMPORT = "PYTHON_IMPORT"
    PYTEST_PATH = "PYTEST_PATH"
    FUNCTION_CALL = "FUNCTION_CALL"
    EXTRACTOR_FIXTURE = "EXTRACTOR_FIXTURE"


@dataclass(frozen=True)
class TestRunResult:
    run_type: TestRunType
    passed: bool
    evidence: dict = field(default_factory=dict)
    error: str | None = None
    blocked: bool = False


DENIED_IMPORTS = frozenset({
    "socket", "ssl", "urllib", "http", "ftplib", "requests", "subprocess",
    "multiprocessing", "ctypes", "pip", "ensurepip", "venv",
})
DENIED_CALLS = frozenset({
    "open", "exec", "eval", "compile", "__import__", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "input", "breakpoint",
})
STDLIB_ALLOWLIST = frozenset({"json", "csv", "re", "math", "typing", "dataclasses"})


class StructuredTestRunner:
    """Perform fixed checks with cwd/path/time/environment confinement."""

    def __init__(self, workspace_root: str | Path, *, timeout_seconds: float = 10.0) -> None:
        self.root = Path(workspace_root).resolve()
        self.scope = ResourceScope.for_root(self.root, create=False)
        self.timeout_seconds = timeout_seconds

    def structural(self, expected_artifacts) -> TestRunResult:
        missing: list[str] = []
        for artifact in expected_artifacts:
            try:
                path = self.scope.resolve(artifact.relative_path)
            except ScopeViolation as exc:
                return TestRunResult(TestRunType.PYTHON_SYNTAX, False, error=str(exc))
            if artifact.required and not path.is_file():
                missing.append(artifact.relative_path)
        return TestRunResult(
            TestRunType.PYTHON_SYNTAX, not missing,
            evidence={"expected": len(expected_artifacts), "missing": missing},
            error=f"missing required artifacts: {missing}" if missing else None,
        )

    def static(self, relative_paths: list[str]) -> TestRunResult:
        checked: list[str] = []
        imports: list[str] = []
        for relative in relative_paths:
            if not relative.endswith(".py"):
                continue
            try:
                path = self.scope.resolve(relative)
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative)
            except (OSError, SyntaxError, ScopeViolation) as exc:
                return TestRunResult(TestRunType.PYTHON_SYNTAX, False, error=str(exc))
            checked.append(relative)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [(node.module or "").split(".")[0]]
                    for name in names:
                        if name in DENIED_IMPORTS:
                            return TestRunResult(TestRunType.PYTHON_IMPORT, False, error=f"network/process import denied: {name}")
                        imports.append(name)
                        if name and name not in STDLIB_ALLOWLIST and not self._local_module(name):
                            if importlib.util.find_spec(name) is None:
                                return TestRunResult(
                                    TestRunType.PYTHON_IMPORT, False,
                                    error=f"dependency {name!r} is unavailable; host installation is forbidden",
                                    blocked=True,
                                )
                            return TestRunResult(
                                TestRunType.PYTHON_IMPORT, False,
                                error=f"import {name!r} is outside the construction allowlist",
                            )
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in DENIED_CALLS:
                    return TestRunResult(TestRunType.PYTHON_IMPORT, False, error=f"dangerous call denied: {node.func.id}")
                if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                    return TestRunResult(TestRunType.PYTHON_IMPORT, False, error="dunder attribute access denied")
        return TestRunResult(
            TestRunType.PYTHON_IMPORT, True,
            evidence={"checked": checked, "imports": sorted(set(imports)), "network": "DENY"},
        )

    def behavior(self, contract: CapabilityContract, implementation_path: str | None) -> TestRunResult:
        if not contract.capability_name:
            return TestRunResult(TestRunType.FUNCTION_CALL, False, error="contract capability_name is required")
        if implementation_path is None:
            # Reuse plans verify their concrete manifest, not arbitrary code.
            manifests = list(self.root.glob("manifests/*.json"))
            if not manifests:
                return TestRunResult(TestRunType.FUNCTION_CALL, False, error="reuse manifest is missing")
            try:
                output = json.loads(manifests[0].read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return TestRunResult(TestRunType.FUNCTION_CALL, False, error=str(exc))
        else:
            try:
                path = self.scope.resolve(implementation_path)
            except ScopeViolation as exc:
                return TestRunResult(TestRunType.FUNCTION_CALL, False, error=str(exc))
            script = (
                "import importlib.util,json,sys\n"
                "p=sys.argv[1]\n"
                "s=importlib.util.spec_from_file_location('candidate',p)\n"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
                "print(json.dumps(m.nexus_capability(json.loads(sys.argv[2]))))\n"
            )
            env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
            env["PYTHONNOUSERSITE"] = "1"
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-S", "-c", script, str(path), json.dumps(contract.input_fixture)],
                    cwd=self.root, env=env, capture_output=True, text=True,
                    timeout=self.timeout_seconds, check=False,
                )
            except subprocess.TimeoutExpired:
                return TestRunResult(TestRunType.FUNCTION_CALL, False, error="capability contract timed out")
            if completed.returncode != 0:
                return TestRunResult(TestRunType.FUNCTION_CALL, False, error=completed.stderr[-1000:])
            try:
                output = json.loads(completed.stdout)
            except ValueError as exc:
                return TestRunResult(TestRunType.FUNCTION_CALL, False, error=f"contract output is not JSON: {exc}")

        reasons = self._assert(contract, output)
        return TestRunResult(
            TestRunType.FUNCTION_CALL, not reasons,
            evidence={"capability": contract.capability_name, "output": output, "assertions": contract.assertions},
            error="; ".join(reasons) if reasons else None,
        )

    def python_tests(self, relative_paths: list[str]) -> TestRunResult:
        """Run zero-argument ``test_*`` functions with a fixed mini-runner.

        This intentionally supports no generated command line, plugin loading,
        package installation or arbitrary cwd.  Capability behavior is still
        checked separately; a green result here is never sufficient for
        VERIFIED (Invariant 100).
        """
        paths: list[str] = []
        try:
            for relative in relative_paths:
                if relative.endswith(".py"):
                    paths.append(str(self.scope.resolve(relative)))
        except ScopeViolation as exc:
            return TestRunResult(TestRunType.PYTEST_PATH, False, error=str(exc))
        if not paths:
            return TestRunResult(TestRunType.PYTEST_PATH, True, evidence={"tests_run": 0})
        script = (
            "import importlib.util,json,sys\n"
            "count=0\n"
            "for i,p in enumerate(sys.argv[1:]):\n"
            " s=importlib.util.spec_from_file_location('sandbox_test_'+str(i),p)\n"
            " m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            " for n in sorted(vars(m)):\n"
            "  f=vars(m)[n]\n"
            "  if n.startswith('test_') and callable(f): f();count+=1\n"
            "print(json.dumps({'tests_run':count}))\n"
        )
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
        env["PYTHONNOUSERSITE"] = "1"
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script, *paths],
                cwd=self.root, env=env, capture_output=True, text=True,
                timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired:
            return TestRunResult(TestRunType.PYTEST_PATH, False, error="sandbox tests timed out")
        if completed.returncode != 0:
            return TestRunResult(TestRunType.PYTEST_PATH, False, error=completed.stderr[-1000:])
        try:
            evidence = json.loads(completed.stdout)
        except ValueError:
            evidence = {"stdout": completed.stdout[-1000:]}
        return TestRunResult(TestRunType.PYTEST_PATH, True, evidence=evidence)

    def _local_module(self, name: str) -> bool:
        return (self.root / f"{name}.py").exists() or (self.root / name / "__init__.py").exists()

    @staticmethod
    def _assert(contract: CapabilityContract, output) -> list[str]:
        reasons: list[str] = []
        for assertion in contract.assertions:
            kind = str(assertion.get("type", ""))
            if kind == CapabilityAssertion.NO_EXCEPTION.value:
                continue
            if kind == CapabilityAssertion.OUTPUT_EXISTS.value and output is None:
                reasons.append("output does not exist")
            elif kind == CapabilityAssertion.OUTPUT_TYPE_EQUALS.value:
                actual = type(output).__name__
                if actual != assertion.get("value", contract.expected_output_type):
                    reasons.append(f"output type {actual!r} did not match")
            elif kind == CapabilityAssertion.JSON_FIELD_EQUALS.value:
                if not isinstance(output, dict) or output.get(assertion.get("field")) != assertion.get("value"):
                    reasons.append(f"JSON field {assertion.get('field')!r} did not match")
            elif kind == CapabilityAssertion.CONTENT_CONTAINS.value:
                if str(assertion.get("value", "")) not in str(output):
                    reasons.append("output content did not contain expected marker")
            elif kind not in {a.value for a in CapabilityAssertion}:
                reasons.append(f"unknown structured assertion {kind!r}")
        if contract.expected_output_type and type(output).__name__ != contract.expected_output_type:
            reasons.append(
                f"output type {type(output).__name__!r} != {contract.expected_output_type!r}"
            )
        return reasons


__all__ = ["StructuredTestRunner", "TestRunResult", "TestRunType"]
