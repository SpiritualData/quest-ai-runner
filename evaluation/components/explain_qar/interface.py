"""The documentation-check function. Every solution is ONE function with this signature:

    explain(kind: str, args: dict) -> dict

Documentation is how a newcomer or an AI learns QAR, so it is held to cases generated from the repository
(``generate_cases.py``): one case per document and per environment variable, so a defect is a failing case with a
name. Everything is offline, no model. Kinds:

  links           {file}      -> {"broken": relative link targets that do not exist}
  em_dash         {file}      -> {"count": em dashes in the file} (house voice rule: none)
  indexed         {file}      -> {"indexed": docs/README.md links the file}
  component_docs  {component} -> {"missing": docs named by that components.csv row that do not exist}
  env_documented  {variable}  -> {"row": has a row in docs/configuration.md, "described": its "What it does" is filled}
  cli_documented  {command}   -> {"documented": the subcommand of the live argparse tree is named in the docs}
  api_documented  {name}      -> {"documented": the name in quest_ai_runner.__all__ is named in README or docs}
  coverage        {}          -> {"uncovered": documents or variables with no case (regenerate the cases)}

An implementation module in ``impls/`` exposes ``build(meter) -> explain``. No solution calls a model.
"""
