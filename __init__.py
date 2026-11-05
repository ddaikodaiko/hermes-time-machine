"""hermes-time-machine — policy-driven workspace checkpoints for Hermes.

Public entry point: `register(ctx)`.
"""

from __future__ import annotations

from . import config as plugin_config
from . import guard, handlers, schemas


def register(ctx):
    handlers.configure(plugin_config.load(ctx))

    ctx.register_tool(
        name="atm_checkpoint",
        toolset="time_machine",
        schema=schemas.ATM_CHECKPOINT,
        handler=handlers.atm_checkpoint,
    )
    ctx.register_tool(
        name="atm_rewind",
        toolset="time_machine",
        schema=schemas.ATM_REWIND,
        handler=handlers.atm_rewind,
    )
    ctx.register_tool(
        name="atm_fork",
        toolset="time_machine",
        schema=schemas.ATM_FORK,
        handler=handlers.atm_fork,
    )
    ctx.register_tool(
        name="atm_list",
        toolset="time_machine",
        schema=schemas.ATM_LIST,
        handler=handlers.atm_list,
    )
    ctx.register_tool(
        name="atm_effects",
        toolset="time_machine",
        schema=schemas.ATM_EFFECTS,
        handler=handlers.atm_effects,
    )

    ctx.register_hook("pre_tool_call", guard.pre_tool_call)
    ctx.register_hook("post_tool_call", guard.post_tool_call)
