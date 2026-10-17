"""BASELINE: an import smoke test. A name case passes when the module imports and the attribute exists, an
offline-import case passes when the module imports, the version case reads the string. Everything that is
BEHAVIOUR (registrations, front doors, helpers, strippers, the email builder, the vendored parser) is
unsupported and returns an empty answer. The floor a surface check must beat. Zero model calls."""
import importlib
import re

import quest_ai_runner


def build(meter):  # noqa: ARG001
    def surface(kind, args):
        if kind == "name":
            obj = getattr(importlib.import_module(args["module"]), args["name"], None)
            return {"found": obj is not None, "callable": callable(obj)}
        if kind == "offline_import":
            importlib.import_module(args["module"])
            return {"imported": True}
        if kind == "version":
            return {"semver": bool(re.match(r"^\d+\.\d+\.\d+", quest_ai_runner.__version__)), "exported": True}
        return {}
    return surface
