"""The public-surface function. Every solution is ONE function with this signature:

    surface(kind: str, args: dict) -> dict

The package a consumer imports is a contract: the names its README and docs promise, the registrations its
root performs, the front doors (``load_config``, ``load_client``), the plumbing helpers a consumer must not
copy, the bootstrap-report email builder, and the vendored third-party code. Everything runs offline with a
scrubbed environment and no model. Kinds and the dict a solution returns:

  name           {module, name}         -> {"found": bool, "callable": bool}
  version        {}                     -> {"semver": bool, "exported": bool}
  hook           {hook}                 -> translator: {"is_cli_model"}; client_factory: {"client"};
                                           config_builders: {"registered": [builder names that resolve]}
  front_door     {door, env}            -> load_client / load_config on a scrubbed environment plus ``env``:
                                           {"client"|"base_url"|"raised": ...}
  loop           {}                     -> {"same_thread", "thread", "propagates"} for run_coro_sync
  detached       {fail}                 -> {"ran", "raised"} for run_detached
  keywords       {fixture}              -> {"names": sorted list | None} for accepted_keywords
  fields         {fixture}              -> {"names": sorted list | None} for accepted_fields
  filter         {kwargs, allowed}      -> {"kept": dict} for filter_keywords
  scrub          {fn, text}             -> {"text"} for the reply-boundary strippers
  email_due      {days_ago, months, form} -> {"due": bool} for should_send_monthly_email
  email_body     {...}                  -> {"text"} for generate_bootstrap_email_body
  offline_import {module}               -> {"imported": bool} in a subprocess whose sockets all raise
  edit           {content, before, after} -> {"text", "ok"} for the vendored apply_edit
  edit_blocks    {content, fn, valid}   -> {"blocks": [[file, before, after] | [file, content]]}

An implementation module in ``impls/`` exposes ``build(meter) -> surface``. No solution here calls a model.
"""
