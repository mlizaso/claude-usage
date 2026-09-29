"""Source-tree compatibility for the historical flat module names."""

import importlib
import sys


def alias_module(alias, target):
    """Make ``alias`` resolve to the exact module object at ``target``."""
    module = importlib.import_module(target)
    sys.modules[alias] = module
    return module
