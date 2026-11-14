"""Locale selection for the add-on's user-facing text."""


def _anki_language_code() -> str:
    """Return the language selected in the active Anki profile."""
    try:
        from aqt import mw

        profile_manager = getattr(mw, "pm", None)
        meta = getattr(profile_manager, "meta", None)
        if isinstance(meta, dict):
            language = meta.get("defaultLang")
            if language:
                return str(language)
    except Exception:
        pass

    try:
        import anki.lang

        language = anki.lang.current_lang
        if callable(language):
            language = language()
        if language:
            return str(language)
    except Exception:
        pass

    # Tests and import-time tooling may load the add-on without an Anki runtime.
    return "zh"


def tr(chinese: str, english: str) -> str:
    """Choose Chinese only for Chinese Anki profiles; use English otherwise."""
    language = _anki_language_code().strip().replace("_", "-").lower()
    primary_language = language.split("-", 1)[0]
    return chinese if primary_language == "zh" else english


def trf(chinese: str, english: str, *args: object, **kwargs: object) -> str:
    """Choose a translation and interpolate its values."""
    return tr(chinese, english).format(*args, **kwargs)
