"""The image-question function (the metric of ``describe-an-attachment``, ``metric-describe-an-attachment``). Every solution is ONE function:

    answer(case: dict) -> dict

``case`` is one image-bearing request (``cases.json``): an image rendered from the case's own spec, and the question the person
asks about it. The answering model is text only (the keyless path, which cannot take an image natively), so what it knows of the
image is what ``prepare_attachments`` puts in its grounding. The function returns:

  reply        the answer to the question
  grounding    the text the answering model was given about the image
  described    whether a model was asked to describe the image

The real ``prepare_attachments`` runs in every arm. An arm decides only whether a vision model describes the image (today, on the
Claude Code subscription) or none is configured (the floor: the image is named and the note says no describer is configured).

Scoring (``score.py``): the reply holds the labelled answer. The gain a description gives is the difference between the two arms'
pass rates on the same cases, read by ``justified_model_spend``; a case whose question the message alone answers is a control, where
a description changes no verdict and only costs. An implementation module in ``impls/`` exposes ``build(meter) -> answer``.
"""
