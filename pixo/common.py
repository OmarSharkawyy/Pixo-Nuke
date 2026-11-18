"""Helpers shared by the Pixo Nuke tools."""
from contextlib import contextmanager

import nuke


@contextmanager
def unmodified():
    """Updating a display must not mark the Nuke script as having unsaved changes."""
    root, was = nuke.root(), None
    try:
        was = root.modified()
    except Exception:  # Older Nuke versions may not expose the modified flag.
        pass
    try:
        yield
    finally:
        if was is not None:
            try:
                root.setModified(was)
            except Exception:
                pass
