"""Override framework symbols without unloading unrelated lazy imports."""

import sys
from contextlib import contextmanager


@contextmanager
def module_overrides(modules):
    """Restore only the specified entries, preserving real dependency caches.

    ``patch.dict(sys.modules, ...)`` restores the entire module registry. That
    can delete lazily imported aiohttp modules while its parent package retains
    references to their classes, splitting class identity on the next import.
    """
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in modules}
    sys.modules.update(modules)
    try:
        yield modules
    finally:
        for name, value in previous.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
