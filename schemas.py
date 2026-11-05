"""Tool schemas exposed to the model."""

ATM_CHECKPOINT = {
    "name": "atm_checkpoint",
    "description": (
        "Create a checkpoint of the protected workspace before a risky change. "
        "Use it before large refactors, migrations, dependency upgrades or destructive commands. "
        "Returns a checkpoint id that atm_rewind and atm_fork accept."
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
        "Restore the workspace to a checkpoint. Without confirm=true it only previews the "
        "restore and lists irreversible external effects recorded since that checkpoint. "
        "Set confirm=true to actually restore files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": "Checkpoint to restore. Defaults to the most recent one.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to perform the restore. Defaults to false (preview only).",
            },
            "allow_irreversible": {
                "type": "boolean",
                "description": (
                    "Proceed even when irreversible external effects happened after the "
                    "checkpoint. Those effects cannot be undone; this only acknowledges them."
                ),
            },
        },
        "required": [],
    },
}

ATM_FORK = {
    "name": "atm_fork",
    "description": (
        "Create an independent copy of the workspace at a checkpoint, so an alternative "
        "approach can be explored without touching the original. Returns the new path."
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
    "description": "List available checkpoints, newest first, with size, file count and trigger.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of checkpoints to return. Defaults to 20.",
            }
        },
        "required": [],
    },
}

ATM_EFFECTS = {
    "name": "atm_effects",
    "description": (
        "List external effects recorded since a checkpoint, such as git push, npm publish "
        "or HTTP writes. Irreversible effects are the reason a rewind may be unsafe."
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
