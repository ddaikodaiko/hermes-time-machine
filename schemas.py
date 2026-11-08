"""Tool schemas exposed to the model.

Every rollback restores files. hermes-time-machine is the one that knows what a
rollback can't undo, so the descriptions tell the model when each tool pays off.
"""

ATM_CHECKPOINT = {
    "name": "atm_checkpoint",
    "description": (
        "Take a restore point of the protected workspace now. Use it before a large refactor, "
        "a migration, a dependency upgrade or any change you may want to undo as a whole. "
        "Snapshots are incremental, so repeating it is cheap. Returns a checkpoint id that "
        "atm_rewind and atm_fork accept. Automatic checkpoints already happen before the first "
        "file change of each turn and before risky commands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": "Short human-readable label, e.g. 'before schema migration'.",
            }
        },
        "required": [],
    },
}

ATM_REWIND = {
    "name": "atm_rewind",
    "description": (
        "Preview or restore the workspace to a checkpoint. Use it when your changes broke the "
        "project and fixing forward is costing more than starting over. Preview first (default); "
        "it shows file changes and any external effects (pushes, publishes) a restore will NOT "
        "undo. Restoring needs confirm=true, plus allow_irreversible=true when such effects exist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": "Checkpoint to restore. Defaults to the most recent one of this workspace.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to perform the restore. Defaults to false (preview only).",
            },
            "allow_irreversible": {
                "type": "boolean",
                "description": (
                    "Proceed even when irreversible external effects happened after the "
                    "checkpoint. Those effects stay in place; this only acknowledges them."
                ),
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restore only these files or folders (relative paths).",
            },
        },
        "required": [],
    },
}

ATM_FORK = {
    "name": "atm_fork",
    "description": (
        "Copy the workspace as it was at a checkpoint into a new sibling folder, so an alternative "
        "approach can be tried without touching the current work. Use it when you want to compare "
        "two approaches instead of overwriting one. Returns the new path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": "Checkpoint to branch from. Defaults to the most recent one.",
            },
            "name": {
                "type": "string",
                "description": "Optional suffix for the new directory name.",
            },
        },
        "required": [],
    },
}

ATM_LIST = {
    "name": "atm_list",
    "description": (
        "List the restore points of this workspace, newest first, with size, file count and what "
        "triggered them. Use it to pick a checkpoint id for atm_rewind or atm_fork."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of checkpoints to return. Defaults to 20.",
            },
            "all_workspaces": {
                "type": "boolean",
                "description": "Include checkpoints of other workspaces. Defaults to false.",
            },
        },
        "required": [],
    },
}

ATM_EFFECTS = {
    "name": "atm_effects",
    "description": (
        "List the external effects recorded since a checkpoint (git push, npm publish, deploys, "
        "HTTP writes), with the exact command that compensates each one. Use it before a rewind or "
        "when you need to remember what already happened outside the workspace: a rewind restores "
        "files but cannot undo these."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "description": "Checkpoint id to look back to. Defaults to the most recent checkpoint.",
            }
        },
        "required": [],
    },
}
