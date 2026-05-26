"""Tool registrations. Auto-imports every non-private module in this package
so each @mcp.tool() decoration runs at import time. Add a new tool by
dropping a file into this directory — no edits to this file required."""

import importlib
import pkgutil

for _finder, _modname, _ in pkgutil.iter_modules(__path__):
    if _modname.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{_modname}")
