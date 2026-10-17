"""The example-check function. Every solution is ONE function with this signature:

    run_example(kind: str, args: dict) -> dict

``examples/`` is how a newcomer learns the library, so each example must still compile, import, run its
offline path, and be introduced in ``examples/README.md``; none may hold a real secret or path. Kinds:

  compile     {file}                 -> {"compiles": bool}
  import      {module}               -> {"imported": bool}
  run         {file, argv, env}      -> {"exit": int, "text": stdout and stderr} in a subprocess whose environment
                                        holds only ``env`` (so no deployment credentials leak in); an example
                                        that needs a live server is run to its "not configured" exit
  stub        {marker}               -> {"action"} from the e2e demo's deterministic ModelProvider
  config_file {}                     -> {"runner_label"} from the shipped ``qar.toml`` via RunnerConfig.from_file
  documented  {file}                 -> {"documented": bool}: ``examples/README.md`` names the file
  clean       {file}                 -> {"hits": [...]}: secret or machine-path patterns found in the file

An implementation module in ``impls/`` exposes ``build(meter) -> run_example``. No solution calls a model.
"""
