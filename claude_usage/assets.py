"""Asset discovery shared by the full and quota-only web surfaces."""

import sys
from pathlib import Path


# Four is one beyond the deepest ordinary purelib-to-data-prefix relation.
PREFIX_SEARCH_DEPTH = 4


def asset_roots(module_file):
    """Return candidate roots for checkout and installed asset layouts."""
    here = Path(module_file).resolve().parent
    roots = [here, here.parent, Path(sys.prefix) / "share" / "claude-usage"]
    roots.extend(parent / "share" / "claude-usage"
                 for parent in list(here.parents)[:PREFIX_SEARCH_DEPTH])
    # Preserve nearest-first order without repeated filesystem probes.
    return list(dict.fromkeys(roots))


def find_asset_dir(module_file, relative_dir, sentinel):
    """Find ``relative_dir`` under a candidate root by its sentinel file."""
    for root in asset_roots(module_file):
        candidate = root / relative_dir
        if (candidate / sentinel).is_file():
            return candidate
    return None
