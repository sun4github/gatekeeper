---
description: "Generate a Python helper to read .json config values with validation and clear errors."
name: "JSON Config Value Reader"
argument-hint: "Provide file path, key or key path, expected type, default behavior, and error policy."
agent: "agent"
---
Related skill: json-config-value-reader. Load and follow SKILL.md.

Create or update a Python helper that reads a configuration file ending in .json and returns the requested value.

Inputs to collect from the user if missing:
- JSON file path
- Requested key or nested key path
- Expected output type (list, dict, string, number, bool)
- Missing-key behavior (default fallback or fail-fast)
- Error style (HTTPException for API surface or internal exception)

Requirements:
1. Validate file extension ends with .json.
2. Read and parse JSON with explicit handling for OSError and json.JSONDecodeError.
3. Resolve requested keys deterministically.
4. Validate the returned type when an expected type is provided.
5. Return clear, stable error messages that include file and key context.
6. Keep implementation small and reusable.

Output format:
1. Short summary of the chosen behavior.
2. Code diff or file edits.
3. Brief validation notes and follow-up test ideas.

If the task references this repository, align with existing patterns in [config](../../app/core/config.py).
