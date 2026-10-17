"""The hygiene-scan function. Every solution is ONE function with this signature:

    scan(kind: str, args: dict) -> dict

This repository is public: no key, token, real machine path, object id or real address may sit in it. A scanner
is judged on two things, both offline. It must FIND planted secrets (seeded positives, assembled from fragments
at run time so this folder never holds a whole one) and stay silent on documented placeholders (the shape of a
secret shown in prose), and the repository itself must come out clean. Kinds:

  text  {parts}              -> {"patterns": sorted pattern names found in ''.join(parts)}
  repo  {include, exclude}   -> {"findings": ["path:line: pattern", ...]} over the files git knows about whose
                                path matches a glob in ``include`` and none in ``exclude``
  cli   {files, gitignore, stage, argv}
                             -> {"exit", "text"}: the scanner run as a command in a throwaway git repo whose
                                files are written from fragment lists; ``stage`` names the files to ``git add``

An implementation module in ``impls/`` exposes ``build(meter) -> scan``. No solution calls a model.
"""
