"""Explicit logging setup confined to the cached_response logger hierarchy."""

import logging

LOGGER = logging.getLogger("cached_response")
_owned_handlers = []


def configure_logging(
    *, enabled=True, path=None, console=False, level=logging.INFO
):
    """Opt in to package logs in a file and/or console; never change root logging.

    Calling with enabled=False removes handlers created by this helper.
    Application-installed handlers are preserved. No destination means silence.
    """
    handlers = []
    if enabled:
        if path is not None:
            handlers.append(logging.FileHandler(path, encoding="utf-8"))
        if console:
            handlers.append(logging.StreamHandler())
    formatter = logging.Formatter(
        "[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s"
    )
    for handler in _owned_handlers:
        LOGGER.removeHandler(handler)
        handler.close()
    _owned_handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
        _owned_handlers.append(handler)
    LOGGER.setLevel(level)
    return LOGGER
