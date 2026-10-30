"""file_modes — give an atomically written file the permissions a normal write would have had.

Moved to the hub package (``hubofhubs.filemodes``); this module re-exports its names so existing imports keep working (CHANGELOG).
"""
from hubofhubs.filemodes import (  # noqa: F401
    umask_lock,
    current_umask,
    match_umask,
)
