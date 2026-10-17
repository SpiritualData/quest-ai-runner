"""Verify a QAR consumer's integration registry: the generic check any system built on QAR can run.

A consumer (a backend that implements QAR ports, passes config, calls the library) lists each
integration point as one row of a CSV. This module checks the rows against the installed library so
drift fails a normal test run. Nothing here knows any particular consumer.

Registry columns (extra columns, such as ``tracking_*``, are ignored):

    integration_id  unique id of the row
    module          the consumer's dotted module path
    symbol          the consumer's class or function in that module (empty for a pure config row)
    qar_solution_id a solution id from QAR's ``docs/structure/solutions.csv`` (empty if none applies)
    qar_port        the QAR port (Protocol class name, such as ``RetrievalAdapter``) an implementation claims
    qar_symbol      for kind=call or kind=config: the QAR ``dotted.module:Name`` the consumer uses
    kind            implementation | config | call | duplicate
    selected_by     how this piece is chosen (config key, selection table, hand-wired)
    metric          an existing eval or live proof, or empty with a gap
    tests           ';'-separated test paths under the root
    status          done | partial | gap
    gap, action     required when status is not done, when kind is duplicate, or when metric is empty

Checks: C1 columns and kinds; C2 solution ids exist; C3 an implementation imports and has every
public method of the port it claims, accepting the port's parameter names; C4 a call or config row's
``qar_symbol`` resolves in the installed library; C5 duplicate rows state an action; C6 status, gap and
action; C7 tests exist; C8 pin lag (reported, never a violation unless the caller asks).

With ``solutions=`` and ``results=`` CSVs the check also enforces that every integration point sits in a
solution (the optional registry column ``solution_id``) with a CURRENT recorded result:

    solutions.csv   solution_id, selected, metric_files (';'-separated paths under the root hashed as the cases)
    results.csv     solution_id, cases, passed, git_sha, cases_sha (the latest row per solution counts; rows
                    with kind=live are live proofs and do not gate)

C9 the row names a solution that exists and has a selected implementation; C10 the solution has a
result with a git sha and a cases hash, every case passing (or the solution's gap says why not), and the hash still matches the metric files
(a stale hash means the cases changed since the run).
"""
from __future__ import annotations

import csv
import hashlib
import importlib
import inspect
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KINDS = ("implementation", "config", "call", "duplicate")
STATUSES = ("done", "partial", "gap")
REQUIRED_COLUMNS = ("integration_id", "module", "symbol", "qar_solution_id", "qar_port", "qar_symbol", "kind",
                    "selected_by", "metric", "tests", "status", "gap", "action")
PIN_RE = re.compile(r"quest[-_]ai[-_]runner\s*@\s*git\+\S+?@([0-9a-fA-F]{7,40})")


@dataclass
class ConsumerReport:
    """What the check found: violations, informational notes, row counts by kind, and pin lag."""
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    pin_lag: bool = False


def library_dir() -> Path:
    """The folder holding the installed ``quest_ai_runner`` package."""
    import quest_ai_runner
    return Path(quest_ai_runner.__file__).resolve().parent


def known_solution_ids() -> Tuple[set, bool]:
    """Solution ids the library defines, and whether the full ``solutions.csv`` was found.

    A source checkout has ``docs/structure/solutions.csv``; a wheel install does not, and then the
    ids come from the selection table (the solutions that have a port).
    """
    csv_path = library_dir().parent / "docs" / "structure" / "solutions.csv"
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as handle:
            return {row["solution_id"] for row in csv.DictReader(handle)}, True
    from ..core.selection_table import SELECTION
    return set(SELECTION), False


def resolve_symbol(dotted: str) -> Any:
    """Import ``package.module:Name`` (or ``package.module.Name``) and return the object."""
    if ":" in dotted:
        module_name, attr = dotted.split(":", 1)
    else:
        module_name, _, attr = dotted.rpartition(".")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


PORT_MODULES = ("quest_ai_runner.core.adapters", "quest_ai_runner.adapters.card_repository",
                "quest_ai_runner.adapters.reference_resolver", "quest_ai_runner.adapters.quest_api_card_repository",
                "quest_ai_runner.adapters.web_search_backends", "quest_ai_runner.runner.feedback_ledger",
                "quest_ai_runner.runner.context_sources_base", "quest_ai_runner.core.inbox",
                "quest_ai_runner.core.recent_context", "quest_ai_runner.core.anticipation_store")


def resolve_port(port: str) -> Any:
    """A port by ``module:Name``, or by bare name searched in the modules that define QAR's ports."""
    if ":" in port or "." in port:
        return resolve_symbol(port)
    for module_name in PORT_MODULES:
        try:
            found = getattr(importlib.import_module(module_name), port, None)
        except Exception:  # noqa: BLE001 - an optional dependency may be absent
            continue
        if found is not None:
            return found
    raise AttributeError(f"{port} is not defined in any of {', '.join(PORT_MODULES)}")


def port_methods(port: Any) -> Dict[str, inspect.Signature]:
    """The public callable members of a Protocol class, with signatures."""
    methods: Dict[str, inspect.Signature] = {}
    for name, member in vars(port).items():
        if name.startswith("_") or not callable(member):
            continue
        try:
            methods[name] = inspect.signature(member)
        except (TypeError, ValueError):
            continue
    return methods


def conformance_problems(cls: Any, port: Any) -> List[str]:
    """Why ``cls`` does not satisfy ``port``: a missing method, or a REQUIRED parameter name it cannot accept.

    A port parameter with a default is an optional capability the library passes only after inspecting
    the implementation's signature, so an implementation that lacks it is behind, not wrong.
    """
    problems: List[str] = []
    for name, wanted in port_methods(port).items():
        have = getattr(cls, name, None)
        if have is None or not callable(have):
            problems.append(f"missing method {name}")
            continue
        try:
            got = inspect.signature(have)
        except (TypeError, ValueError):
            continue
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in got.parameters.values()):
            continue
        accepted = set(got.parameters)
        for pname, param in wanted.parameters.items():
            if pname in ("self", "cls") or param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue
            if param.default is not inspect.Parameter.empty:
                continue  # optional capability: the library probes the signature before passing it
            if pname not in accepted:
                problems.append(f"{name} does not accept parameter {pname!r}")
    return problems


def read_pin(requirements: Path) -> Optional[str]:
    """The commit a requirements file pins QAR to, or None."""
    if not requirements.exists():
        return None
    match = PIN_RE.search(requirements.read_text(encoding="utf-8"))
    return match.group(1).lower() if match else None


def installed_commit() -> Optional[str]:
    """HEAD of the QAR checkout the library imports from, or None for a non-git install."""
    try:
        out = subprocess.run(["git", "-C", str(library_dir()), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip().lower() if out.returncode == 0 and out.stdout.strip() else None


def commits_behind(pin: str) -> Optional[int]:
    """How many commits the installed checkout is ahead of the pin (None when the pin is unknown to it)."""
    try:
        out = subprocess.run(["git", "-C", str(library_dir()), "rev-list", "--count", f"{pin}..HEAD"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None


def check_pin(report: ConsumerReport, requirements: Path) -> None:
    """C8: compare the consumer's pinned QAR commit with the installed checkout."""
    pin = read_pin(requirements)
    if pin is None:
        report.notes.append(f"no QAR commit pin found in {requirements}")
        return
    head = installed_commit()
    if head is None:
        report.notes.append(f"pinned QAR {pin[:10]}; installed library is not a git checkout, lag unknown")
        return
    if head.startswith(pin) or pin.startswith(head):
        report.notes.append(f"pinned QAR {pin[:10]} matches the installed checkout")
        return
    behind = commits_behind(pin)
    report.pin_lag = True
    detail = f"{behind} commits behind" if behind is not None else "pin not found in the installed checkout"
    report.notes.append(f"pin lag: requirements pin QAR {pin[:10]}, installed checkout is {head[:10]} ({detail})")


def check_row(row: Dict[str, str], root: Path, solutions: set, report: ConsumerReport) -> None:
    """Run C2 to C7 on one registry row."""
    rid = row.get("integration_id", "?")
    kind = row["kind"].strip()
    if kind not in KINDS:
        report.problems.append(f"C1 {rid}: kind {kind!r} is not one of {', '.join(KINDS)}")
        return
    report.counts[kind] = report.counts.get(kind, 0) + 1
    solution = row["qar_solution_id"].strip()
    if solution and solution not in solutions:
        report.problems.append(f"C2 {rid}: solution id {solution!r} is not defined by the installed QAR")
    port = row["qar_port"].strip()
    if kind == "implementation":
        if not row["symbol"].strip():
            report.problems.append(f"C3 {rid}: an implementation row needs a symbol")
        else:
            try:
                cls = resolve_symbol(f"{row['module'].strip()}:{row['symbol'].strip()}")
            except Exception as exc:  # noqa: BLE001 - any import failure is the finding
                report.problems.append(f"C3 {rid}: {row['module']}:{row['symbol']} does not import ({type(exc).__name__}: {exc})")
                cls = None
            if cls is not None and port:
                try:
                    port_cls = resolve_port(port)
                except Exception:  # noqa: BLE001
                    report.problems.append(f"C3 {rid}: port {port!r} is not defined by the library")
                else:
                    for why in conformance_problems(cls, port_cls):
                        report.problems.append(f"C3 {rid}: {row['symbol']} does not satisfy {port}: {why}")
            elif cls is not None and not (row["gap"].strip() and row["action"].strip()):
                report.problems.append(f"C3 {rid}: an implementation row must name the port it claims, or state the gap "
                                       f"(the library has no Protocol for it) and the action")
    elif kind in ("call", "config"):
        qar_symbol = row["qar_symbol"].strip()
        if not qar_symbol:
            report.problems.append(f"C4 {rid}: a {kind} row needs a qar_symbol")
        else:
            try:
                resolve_symbol(qar_symbol)
            except Exception as exc:  # noqa: BLE001
                report.problems.append(f"C4 {rid}: qar_symbol {qar_symbol} does not resolve ({type(exc).__name__}: {exc})")
        if row["module"].strip():
            try:
                importlib.import_module(row["module"].strip())
            except Exception as exc:  # noqa: BLE001
                report.problems.append(f"C4 {rid}: module {row['module']} does not import ({type(exc).__name__}: {exc})")
    elif not row["action"].strip():
        report.problems.append(f"C5 {rid}: a duplicate row must state the action that removes it")
    status = row["status"].strip()
    if status not in STATUSES:
        report.problems.append(f"C6 {rid}: status {status!r} is not one of {', '.join(STATUSES)}")
    elif status != "done" and not (row["gap"].strip() and row["action"].strip()):
        report.problems.append(f"C6 {rid}: status {status} needs both a gap and an action")
    if not row["metric"].strip() and not (row["gap"].strip() and row["action"].strip()):
        report.problems.append(f"C6 {rid}: no metric, so the gap and the action must say what proof is missing")
    for test in [t.strip() for t in row["tests"].split(";") if t.strip()]:
        if not (root / test.split("::")[0]).exists():
            report.problems.append(f"C7 {rid}: test {test} does not exist under {root}")


def cases_hash(files: List[str], root: Path) -> str:
    """sha256 (first 12 hex) over the named files' bytes, in sorted order: the identity of a case set."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode())
        digest.update((Path(root) / name).read_bytes())
    return digest.hexdigest()[:12]


def read_rows(path: Path) -> List[Dict[str, str]]:
    """A CSV as a list of dicts with empty-string defaults."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [{k: (v or "") for k, v in row.items() if k} for row in csv.DictReader(handle)]


def check_solutions(report: ConsumerReport, rows: List[Dict[str, str]], solutions: Path, results: Path, root: Path) -> None:
    """C9 and C10: every integration point sits in a solution with a current recorded result."""
    sols = {r["solution_id"]: r for r in read_rows(solutions)}
    latest: Dict[str, Dict[str, str]] = {}
    for r in read_rows(results):
        if r.get("kind", "offline") == "live":
            continue  # a live proof is recorded beside the gating offline metric, never instead of it
        latest[r["solution_id"]] = r
    used = set()
    for row in rows:
        sid = row.get("solution_id", "").strip()
        rid = row["integration_id"]
        if not sid:
            report.problems.append(f"C9 {rid}: the integration point sits in no solution (solution_id is empty)")
        elif sid not in sols:
            report.problems.append(f"C9 {rid}: solution {sid!r} is not in {solutions.name}")
        else:
            used.add(sid)
    for sid in sorted(used):
        sol = sols[sid]
        if not sol.get("selected", "").strip():
            report.problems.append(f"C9 {sid}: no selected implementation")
        res = latest.get(sid)
        if res is None:
            report.problems.append(f"C10 {sid}: no recorded result")
            continue
        if not res.get("git_sha", "").strip() or not res.get("cases_sha", "").strip():
            report.problems.append(f"C10 {sid}: the latest result lacks a git sha or a cases hash")
        if res.get("passed", "") != res.get("cases", "x"):
            if sol.get("gap", "").strip():
                report.notes.append(f"{sid}: latest result {res.get('passed')}/{res.get('cases')}, the solution's gap names why")
            else:
                report.problems.append(f"C10 {sid}: latest result {res.get('passed')}/{res.get('cases')} is not all passing and the solution states no gap")
        files = [f for f in sol.get("metric_files", "").split(";") if f.strip()]
        if files and all((root / f).exists() for f in files) and cases_hash(files, root) != res.get("cases_sha", ""):
            report.problems.append(f"C10 {sid}: stale result, the metric files changed since it was recorded")
    report.notes.append(f"{len(used)} solutions carry integration points; {len(sols) - len(used)} defined without one")


def check_consumer(registry: Path, root: Path, requirements: Optional[Path] = None,
                   solutions: Optional[Path] = None, results: Optional[Path] = None) -> ConsumerReport:
    """Check every row of a consumer registry against the installed QAR; optionally report pin lag."""
    report = ConsumerReport()
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    with Path(registry).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            report.problems.append(f"C1 {registry}: missing columns {', '.join(missing)}")
            return report
        rows = [{k: (v or "") for k, v in row.items() if k} for row in reader]
    known, full = known_solution_ids()
    if not full:
        report.notes.append("solutions.csv not found next to the library; solution ids checked against the selection table only")
    seen = set()
    for row in rows:
        rid = row["integration_id"].strip()
        if rid in seen:
            report.problems.append(f"C1 {rid}: duplicate integration_id")
        seen.add(rid)
        check_row(row, root, known, report)
    if solutions is not None and results is not None:
        check_solutions(report, rows, Path(solutions), Path(results), root)
    if requirements is not None:
        check_pin(report, Path(requirements))
    return report
