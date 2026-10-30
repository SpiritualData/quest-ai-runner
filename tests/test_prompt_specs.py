"""Enforces the prompt standard (core/prompt_specs.py): every model call goes through a registered
prompt spec, or sits in the shrinking baseline of sites not yet migrated; every spec names a real
ESOI solution and one of that solution's metrics. Offline, no model call."""
from __future__ import annotations

import ast
import csv
import importlib
import pkgutil
from pathlib import Path

import pytest

import quest_ai_runner
from quest_ai_runner.core import prompt_specs
from quest_ai_runner.core.prompt_specs import NO_EM_DASH_RULE, PromptSpec, render_prompt

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "quest_ai_runner"
BASELINE = Path(__file__).resolve().parent / "prompt_spec_baseline.txt"

MODEL_METHODS = {"answer", "plan", "web_search", "fetch_url"}
MODEL_FUNCTIONS = {"answer_with_reasoning", "plan_with_step", "call_judge", "ask_judge"}
# Calls that take a registered prompt id (a string literal) and render it.
SPEC_REFERENCES = {"render_prompt", "get_spec", "ask_judge"}
# Provider transport and tool-use plumbing: they carry a caller's prompt, they do not own one.
TRANSPORT = {
    "adapters/claude_cli_provider.py",
    "adapters/web_search_backends.py", "adapters/provider_web_search_adapter.py",
    "adapters/web_research.py",
}


def read_csv(name: str) -> list:
    with open(REPO / "esoi" / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def is_model_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in MODEL_METHODS:
            return True
        owner = func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        return func.attr == "generate" and owner_name.endswith("provider")
    return isinstance(func, ast.Name) and func.id in MODEL_FUNCTIONS


def forwards_prompt(call: ast.Call, stack: list) -> bool:
    """A call inside a function that takes the finished ``prompt`` as a parameter and passes it on
    is a pass-through (a nested callable only): the caller that builds the prompt owns the spec, and is scanned itself."""
    functions = [s for s in stack if not isinstance(s, ast.ClassDef)]
    function = functions[-1] if len(functions) > 1 else None  # only a nested callable
    if function is None or not any(a.arg == "prompt" for a in function.args.args):
        return False
    return any(isinstance(n, ast.Name) and n.id == "prompt" for a in call.args for n in ast.walk(a))


def scan_sources() -> tuple:
    """(sites, rendered): unit keys that call a model, and unit keys that render a spec id."""
    sites, rendered = set(), set()
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if "vendor" in path.parts or rel in TRANSPORT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, stack):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(child, stack + [child])
                    continue
                if isinstance(child, ast.Call):
                    names = [s.name for s in stack]
                    unit = ""
                    for index, scope in enumerate(stack):
                        if not isinstance(scope, ast.ClassDef):
                            unit = ".".join(names[: index + 1])
                            break
                    key = f"{rel}::{unit}"
                    if unit and is_model_call(child) and not forwards_prompt(child, stack):
                        sites.add(key)
                    func = child.func
                    if unit and isinstance(func, ast.Name) and func.id in SPEC_REFERENCES:
                        for arg in child.args[:2]:
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                rendered.add((key, arg.value))
                walk(child, stack)

        walk(tree, [])
    return sites, rendered


def import_every_module() -> None:
    for info in pkgutil.walk_packages(quest_ai_runner.__path__, "quest_ai_runner."):
        if ".vendor" in info.name:
            continue
        try:
            importlib.import_module(info.name)
        except ImportError:
            continue


def baseline() -> set:
    lines = BASELINE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def test_every_model_call_goes_through_a_registered_spec_or_the_baseline():
    import_every_module()
    sites, rendered = scan_sources()
    registered_sites = {key for key, spec_id in rendered if spec_id in prompt_specs.SPECS_BY_ID}
    unknown = sorted(f"{key} -> {spec_id}" for key, spec_id in rendered
                     if spec_id not in prompt_specs.SPECS_BY_ID)
    assert not unknown, f"render_prompt with an unregistered id: {unknown}"
    allowed = baseline()
    new = sorted(sites - registered_sites - allowed)
    assert not new, ("model call not going through a registered prompt spec (declare a PromptSpec "
                     "in core/prompt_specs.py style and call render_prompt with its id): " + str(new))
    stale = sorted(allowed - (sites - registered_sites))
    assert not stale, ("baseline entry no longer needed (migrated or gone); delete the line: "
                       + str(stale))


def test_every_spec_names_a_real_solution_and_one_of_its_metrics():
    import_every_module()
    solutions = {r["solution_id"]: r for r in read_csv("solutions.csv")}
    metrics = {r["metric_id"] for r in read_csv("metrics.csv")}
    assert prompt_specs.SPECS_BY_ID, "no prompt spec registered"
    for spec in prompt_specs.SPECS_BY_ID.values():
        assert spec.solution in solutions, f"{spec.id}: unknown solution {spec.solution}"
        assert spec.metric in metrics, f"{spec.id}: unknown metric {spec.metric}"
        linked = [m for m in solutions[spec.solution]["metric_ids"].split(";") if m]
        assert spec.metric in linked, f"{spec.id}: {spec.solution} does not link {spec.metric}"


def test_render_carries_the_no_em_dash_rule_once_and_checks_inputs():
    spec = PromptSpec(id="t.demo", solution="s", metric="m", tier="fast", cache_layer="none",
                      template="Say hi to {name}.", inputs=("name",))
    text = spec.render(name="Ann")
    assert text.endswith(NO_EM_DASH_RULE) and text.count(NO_EM_DASH_RULE) == 1
    assert spec.render(name="Ann") == text
    with pytest.raises(ValueError):
        spec.render()
    with pytest.raises(ValueError):
        PromptSpec(id="t.bad", solution="s", metric="m", tier="huge", cache_layer="none", template="x")
    assert render_prompt  # the lookup entry point exists
