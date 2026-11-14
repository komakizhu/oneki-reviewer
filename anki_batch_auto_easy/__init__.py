"""Anki batch-based automatic Good-to-Easy add-on."""

try:
    import aqt  # type: ignore
except ImportError:
    aqt = None

if aqt is not None:
    from .integration import initialize

    initialize()

