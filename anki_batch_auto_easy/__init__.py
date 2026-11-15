"""Anki add-on entry point with classic and V3 scheduler paths."""

import sys

try:
    import aqt  # type: ignore
except ImportError:
    aqt = None


def _initialize_for_runtime():
    if aqt is None:
        return

    # Anki 2.1.0-2.1.49 uses Python 3.6 and the classic scheduler. The
    # modern implementation uses syntax and APIs that do not exist there.
    # Later releases can also use the classic scheduler, so inspect the
    # active collection after a profile has opened.
    if sys.version_info < (3, 7) or not hasattr(aqt, "gui_hooks"):
        from .legacy import initialize

        initialize()
        return

    from aqt import mw

    collection = getattr(mw, "col", None)
    scheduler = getattr(collection, "sched", None)
    if scheduler is None:
        hooks = getattr(aqt, "gui_hooks", None)
        profile_hook = getattr(hooks, "profile_did_open", None)
        if profile_hook is None:
            from .integration import initialize

            initialize()
            return
        if getattr(mw, "_anki_batch_auto_easy_waiting_for_profile", False):
            return
        mw._anki_batch_auto_easy_waiting_for_profile = True

        def after_profile_open(*_args):
            try:
                profile_hook.remove(after_profile_open)
            except (AttributeError, ValueError):
                pass
            mw._anki_batch_auto_easy_waiting_for_profile = False
            _initialize_for_runtime()

        profile_hook.append(after_profile_open)
        return

    if not callable(getattr(scheduler, "answer_card", None)):
        from .legacy import initialize

        initialize()
    else:
        from .integration import initialize

        initialize()


_initialize_for_runtime()
