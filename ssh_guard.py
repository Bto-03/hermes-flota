"""Keep a Flota machine's own ``~/.hermes`` out of Hermes' SSH file sync.

Core's SSH terminal (``tools/environments/ssh.py``) mirrors the local skills, credentials and caches
into ``<remote $HOME>/.hermes`` when it connects, and on cleanup tars that whole directory back
(``tar cf - -C / home/<user>/.hermes``, a 120 s timeout, 3 tries). That suits a throwaway box. A
Flota machine is one of your own computers: its ``~/.hermes`` is a real Hermes install (venvs,
caches, often many GB), so the push writes this host's files into it and the pull times out after
holding the parent busy for minutes. No ``terminal.*`` key turns it off, but the constructor
takes ``sync_files``; this passes ``sync_files=False`` for SSH terminals opened as a
``flota-<machine>`` profile (that is how core runs a Flota subagent). Other profiles are untouched.
"""
from __future__ import annotations

import functools
import inspect
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROFILE_PREFIX = "flota-"
_MARK = "_flota_no_file_sync"


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def is_flota_profile(home: Path) -> bool:
    return home.parent.name == "profiles" and home.name.startswith(PROFILE_PREFIX)


def _core_class():
    from tools.environments.ssh import SSHEnvironment
    return SSHEnvironment


def installed(cls=None) -> bool:
    try:
        cls = cls or _core_class()
    except ImportError:
        return False
    return getattr(cls.__init__, _MARK, False)


def install(cls=None) -> bool:
    """Wrap ``cls.__init__`` (core's SSHEnvironment by default). False when that is impossible."""
    try:
        cls = cls or _core_class()
    except ImportError:
        logger.warning("flota: tools.environments.ssh not found; SSH file sync is not guarded")
        return False
    if installed(cls):
        return True
    original = cls.__init__
    if "sync_files" not in inspect.signature(original).parameters:
        logger.warning("flota: SSHEnvironment has no sync_files parameter; a flota profile's SSH "
                       "terminal may copy the remote ~/.hermes back")
        return False

    @functools.wraps(original)
    def __init__(self, *args, **kwargs):
        try:
            flota = is_flota_profile(_hermes_home())
        except Exception:  # never break a terminal over this check
            flota = False
        if flota:
            kwargs["sync_files"] = False
        original(self, *args, **kwargs)

    setattr(__init__, _MARK, True)
    cls.__init__ = __init__
    return True
