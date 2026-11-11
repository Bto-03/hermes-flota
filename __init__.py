"""Flota: a ``delegation.backend`` that runs each subagent on one of your own machines.

The agent picks a ``machine`` per ``delegate_task`` task. Flota checks that the machine answers,
wakes it when it does not (Wake-on-LAN, a relay host, or any command), then routes the subagent to
the Hermes profile whose terminal is SSH on that machine. When the machine's last subagent ends it
is put back to sleep. ``/flota`` shows the fleet; ``hermes flota setup`` adds a machine.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _machine(name: str):
    """The machine as configured right now, or None when it was removed."""
    from . import tool
    from .machines import load_machines
    return load_machines(tool._effective(tool._load_config() or {}))[0].get(name)


def register(ctx) -> None:
    try:
        from .provider import FlotaDelegationProvider
    except ModuleNotFoundError as exc:
        if exc.name != "agent.delegation_provider":
            raise
        logger.warning("flota needs a Hermes core with pluggable delegation providers "
                       "(agent.delegation_provider); update Hermes. The plugin is inactive until then.")
        return
    from . import cli, slash, ssh_guard, tool
    from .idle import IdleSleeper, state_path

    ssh_guard.install()  # flota-<machine> SSH terminals: no copy of the machine's ~/.hermes

    # shortcut: one state file per profile; two processes running subagents at once (gateway and a
    # chat session) overwrite each other's counts, upgrade to one file per pid if that matters.
    sleeper = IdleSleeper(state_file=state_path, reload=_machine)
    ctx.register_delegation_provider(FlotaDelegationProvider(sleeper=sleeper))
    ctx.register_hook("subagent_start", sleeper.on_start)
    ctx.register_hook("subagent_stop", sleeper.on_stop)
    ctx.register_tool(name="flota", toolset="flota", schema=tool.SCHEMA,
                      handler=tool.make_handler(sleeper=sleeper), check_fn=tool.available,
                      description="Check, wake, sleep or power off your machines", emoji="🖥️")
    ctx.register_command("flota", slash.make_handler(ctx, sleeper=sleeper),
                         description="Show your machines, or dry-run / wake / sleep / power off one",
                         args_hint="[status | dry-run <machine> | wake <machine> | sleep <machine> | off <machine>]")
    ctx.register_cli_command(
        name="flota", help="Flota: run subagents on your own machines",
        setup_fn=cli.setup_parser, handler_fn=cli.handle,
        description="`hermes flota setup <name> --host H --user U` adds a machine; "
                    "`hermes flota doctor [machine]` checks it and prints the fix for each failed line; "
                    "`hermes flota status | dry-run | wake | sleep | off <machine>` inspect and control it; "
                    "`hermes flota invite` / `join <code>` / `add-ship <code>` pair another machine; "
                    "tune it in Plugins > Flota.")
