#!/usr/bin/env python3
"""PreToolUse hook: block Write/Edit that introduce forbidden Verus patterns.

Returns "ask" permission so the user gets prompted to approve or deny.
"""

import json
import re
import sys


def ask_permission(reason: str):
    """Output JSON that triggers the interactive permission prompt."""
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


data = json.load(sys.stdin)
tool = data.get("tool_name", "")

if tool == "Write":
    content = data.get("tool_input", {}).get("content", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
elif tool == "Edit":
    content = data.get("tool_input", {}).get("new_string", "")
    file_path = data.get("tool_input", {}).get("file_path", "")
else:
    sys.exit(0)

# Only check .rs files
if not file_path.endswith(".rs"):
    sys.exit(0)

CHECKS = [
    (r'\bassume\s*\(', "assume(...)"),
    (r'\badmit\s*\(',  "admit()"),
    (r'verifier\s*::\s*external_body', "#[verifier::external_body]"),
]

for line in content.splitlines():
    stripped = line.lstrip()
    if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
        continue
    code_part = line.split("//")[0]

    for regex, name in CHECKS:
        if re.search(regex, code_part):
            ask_permission(
                f"Code contains `{name}`. This is forbidden -- "
                "the goal is full end-to-end verification. "
                "Allow only if genuinely unavoidable."
            )

sys.exit(0)
