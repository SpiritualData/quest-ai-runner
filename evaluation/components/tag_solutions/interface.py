"""The solution-tag function. Every implementation is ONE function with this signature:

    tags(kind: str, args: dict) -> dict

The solution tag-solutions keeps a flat list of tags (``esoi/tags.csv``: tag, meaning, the problems it mitigates) and
the tags column of each solution. What is measured here, offline, is the mechanical half of its first milestone: the
rule fails every tag list a person could get wrong without judgement, passes a good one, and the lookup finds a
solution through its tags and through its connected problems. Whether a tag mitigates its risk (a solution missed
without it on labelled requests) is the planned half of the metric and is not measured here. Kinds:

  check   {tags_csv, solutions, problems}
          -> {"problems": the rule's problem lines, "notes": its notes}; ``tags_csv`` is the text of a tags file,
             written to a scratch folder and read as the rule reads ``esoi/tags.csv``; ``solutions`` are records with
             solution_id, tags and problem_ids; ``problems`` are the ids of the problem records
  lookup  {solutions, ids}
          -> {"found": the ids of the solutions that carry any of ``ids`` (tags or problem ids), in record order}

An implementation module in ``impls/`` exposes ``build(meter) -> tags``. No implementation calls a model.
"""
