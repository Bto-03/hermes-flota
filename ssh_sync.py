"""Keep a Flota machine's own ``~/.hermes`` out of Hermes' SSH file sync, through config only.

Core's SSH terminal mirrors this host's skills, credentials and caches into ``<remote $HOME>/.hermes``
when it connects, and on cleanup tars that whole directory back. That suits a throwaway box. A Flota
machine is one of your own computers: its ``~/.hermes`` is often a real Hermes install (venvs,
caches, many GB), so the pull is slow and can time out.

Flota writes ``terminal.ssh_sync_files: false`` into the config of every machine profile it creates
(``setup``, ``add-ship``) and, with ``hermes flota doctor --fix``, into the ones that already exist.
That key is proposed in core (issue #148726, PRs #148791 and #145271, with ``TERMINAL_SSH_SYNC_FILES``
as its env var); a core that does not read it yet keeps syncing, and ``doctor`` says so. Flota never
touches core classes to get there.
"""
from __future__ import annotations

from collections.abc import Mapping

KEY = "ssh_sync_files"
ISSUE = "https://github.com/NousResearch/hermes-agent/issues/148726"


def disable(terminal: dict) -> bool:
    """Set ``ssh_sync_files: false`` in a profile's ``terminal`` block; True when it changed."""
    if terminal.get(KEY) is False:
        return False
    terminal[KEY] = False
    return True


def disabled(config: Mapping) -> bool:
    """A profile config that already turns SSH file sync off."""
    terminal = config.get("terminal") if isinstance(config, Mapping) else None
    return isinstance(terminal, Mapping) and terminal.get(KEY) is False


def core_reads_key() -> bool:
    """Whether this Hermes core knows ``terminal.ssh_sync_files``, judged from the public
    ``hermes_cli.config.DEFAULT_CONFIG``. A core that reads the key without listing a default for it
    reads as False here, so the answer errs towards "sync is still on"."""
    try:
        from hermes_cli.config import DEFAULT_CONFIG
    except Exception:
        return False
    terminal = DEFAULT_CONFIG.get("terminal") if isinstance(DEFAULT_CONFIG, Mapping) else None
    return isinstance(terminal, Mapping) and KEY in terminal
