"""The lane-configuration function. Every solution is ONE function with this signature:

    configure(kind: str, args: dict) -> dict

A lane is configured by a TOML file plus environment variables, and the layering has to be predictable: the file
is a baseline, an environment variable beats the file, a process variable beats everything in the file's own
``env_*`` tables, a typo is rejected loudly, and a malformed tuning value falls back to the shipped default
instead of crashing a lane at 3 a.m. All of it is deterministic, so this component runs offline.

Kinds and the dict a solution returns:
  file_fields    args {toml}                      -> {"error": exception name | None, "fields": {name: value},
                                                      "personas": skills_root | None}
                 the file parsed alone (``RunnerConfig.from_file``); ``fields`` lists the scalar fields the case names
  layered        args {toml, process, files, probe, fields}
                 -> {"fields": {name: value}, "environ": {probe var: value | None}, "error": ...}
                 the whole startup path (``_config_from_env`` with the file, ``env_files``, ``env_aliases``, ``env``):
                 ``process`` is the variables already set, ``files`` maps a file name to its text (``{dir}`` in the
                 TOML is the folder they are written to), ``probe`` names the variables to report afterwards
  core_settings  args {env}                        -> {"settings": {field: value}}
                 the tuning values core reads (``core_settings_from_env``)

An implementation module in ``impls/`` exposes ``build(meter) -> configure``. No solution here calls a model.
"""
