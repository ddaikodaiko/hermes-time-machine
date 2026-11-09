"""hermes-time-machine: checkpoints, a point-of-no-return gate and effect memory for Hermes.

Every rollback restores files. hermes-time-machine is the one that knows what a
rollback can't undo.

Public entry point: `register(ctx)`.
"""

from __future__ import annotations

from . import (  # noqa: F401  (submodules are re-exported for callers and tests)
    commands,
    dashboard_data,
    effects,
    gate,
    guard,
    handlers,
    memory,
    policy,
    schemas,
    snapshot,
    storage,
)
from . import config as plugin_config

_TOOLS = (
    ("atm_checkpoint", schemas.ATM_CHECKPOINT, handlers.atm_checkpoint),
    ("atm_rewind", schemas.ATM_REWIND, handlers.atm_rewind),
    ("atm_fork", schemas.ATM_FORK, handlers.atm_fork),
    ("atm_list", schemas.ATM_LIST, handlers.atm_list),
    ("atm_effects", schemas.ATM_EFFECTS, handlers.atm_effects),
    ("atm_diff", schemas.ATM_DIFF, handlers.atm_diff),
)

_HOOKS = (
    ("pre_tool_call", guard.pre_tool_call),
    ("post_tool_call", guard.post_tool_call),
    ("transform_tool_result", guard.transform_tool_result),
    ("pre_llm_call", guard.pre_llm_call),
    ("post_compaction", guard.post_compaction),
)


def register(ctx):
    handlers.configure(plugin_config.load(ctx))

    for name, schema, handler in _TOOLS:
        ctx.register_tool(name=name, toolset="time_machine", schema=schema, handler=handler)

    for hook_name, callback in _HOOKS:
        ctx.register_hook(hook_name, callback)

    commands.register(ctx)
