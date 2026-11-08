"""The four tools the milestone flow runs on, built over a host's own Quest client.

Role: a flow step of ESOI Application. The milestone flow (``milestone_cue``) finds the milestone a short cue concerns and records its keyed next step through four tools of
the turn's registry: ``read_spec_records``, ``list_quest_goals``, ``create_or_update_quest_goal`` and, when the host can read a metric's source, ``read_metric_source``. A lane
gets them from its environment (``QAR_BASE_URL`` and a key). A host that runs the brain inside its own service has no such environment: it hands this function a client factory
that answers the Quest client's calls in process, and, for the metric sources, a reader. Nothing else about the tools differs, so the guards that keep a person's goal untouched
and the receipts that say what was read back are the lane's own.

The client the factory returns needs: ``get_spec``, ``get_quest_autopilot`` (optional), ``list_quest_goals``, ``list_assignable_members``, ``create_goal``, ``update_goal`` and
``assign_goal``, with the shapes ``QuestClient`` documents (a write returns a ``WriteResult`` or raises a ``QuestWriteError``).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, List, Mapping, Optional, Sequence

from quest_ai_runner.the_body.hands.quest_operations.tools_quest_goals import create_or_update_quest_goal_spec
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_intents import read_spec_records_spec
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import list_quest_goals_spec
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.tools_metric_source import metric_reader_spec, recorded_reader_spec
from quest_ai_runner.the_world.tools.tools import ToolSpec


def milestone_flow_tools(client_factory: Callable[[], Any], metric_reader: Optional[Callable[[str], Mapping[str, Any]]] = None,
                         metric_source_names: Optional[Sequence[str]] = None,
                         recorded_reader: Optional[Callable[[str, str], Mapping[str, Any]]] = None) -> List[ToolSpec]:
    """The milestone flow's tools over ``client_factory() -> client``. ``metric_reader(name) -> {"value", "as_of", "source", "detail"}`` (raising ``MetricSourceError`` in plain words
    for a source it cannot read) adds ``read_metric_source``; leave it out when the host reads no source. ``recorded_reader(quest_id, metric_id) -> reading`` adds
    ``read_recorded_metric_value``, the latest value recorded for the metric record in the host's own store, which the flow asks before it runs the record's named source."""
    # The goal write and the metric reads belong to the flow's code step, which the milestone cue starts: the planner of a turn the cue did not claim is never shown them
    # (``flow_owned``), so an ordinary request ("write an Instagram post", "remind me every Monday") is not turned into a goal. The two reads below stay offered.
    specs = [read_spec_records_spec({}, client_factory), list_quest_goals_spec({}, client_factory),
             dataclasses.replace(create_or_update_quest_goal_spec({}, client_factory), on_request_only=True, flow_owned=True)]
    if metric_reader is not None:
        specs.append(dataclasses.replace(metric_reader_spec(metric_reader, metric_source_names), on_request_only=True, flow_owned=True))
    if recorded_reader is not None:
        specs.append(dataclasses.replace(recorded_reader_spec(recorded_reader), on_request_only=True, flow_owned=True))
    return specs
