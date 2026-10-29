"""The hub-of-hubs function (the metric of ``use-open-source-hubs``). Every solution is ONE function:

    measure(kind: str, args: dict) -> dict

QAR should sit on top of open-source hubs and pick up what they support, with no code per integration. The registry is
data (``esoi/hubs.csv``, ``esoi/hub_findings.csv``, ``esoi/hub_capabilities.csv``); this component reads it and the code
and answers, per area, whether the hub-of-hubs idea holds. Each case is an ideal: it expects ``holds`` true. Kinds, all
with ``args {area}`` and all returning ``{holds, ...detail}``:

  registered   a hub is built on or compared, or "no hub exists" is a recorded decision, with a research finding
  by_config    at least half of the area's capabilities arrive through a hub by configuration alone
  fresh        the area was researched within the staleness bar (90 days, the metric-run-policy staleness idiom)
  seam         the area has one registry in code: a solution with at least one declared implementation
  selectable   the area's seam solution has an authoritative metric and two or more implementations, so
               ``esoi select --promote`` can choose between them
  core_clean   no vendor-specific branch of the area's vendors in ``quest_ai_runner/core`` (static scan, ``scan.py``)
  fake_hub     a fake hub registered through the area's own configuration entry works with zero core edits

``aggregate`` prints the three metric values (coverage, integration-by-config share, research freshness: max and mean
days) and the code-structure values, and the combined score. An implementation module in ``impls/`` exposes
``build(meter) -> measure``. No model call, no network.
"""
