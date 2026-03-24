---
name: json-config-value-reader
description: "Read configuration files ending in .json and return corresponding values with clear validation and error handling. Use for config loaders, startup settings, and API-safe JSON lookup helpers in Python projects."
argument-hint: "Describe the JSON file path, expected key(s), default behavior, and error policy."
user-invocable: true
---

# JSON Config Value Reader

## What This Skill Produces
- A deterministic Python helper that reads a .json config file and returns one or more requested values.
- Explicit handling for file I/O and JSON parsing failures.
- Predictable behavior for missing keys: default fallback or raised HTTPException.
- Optional shape validation for list and dict payloads.

## When To Use
- You need to load startup configuration from files like services_config.json or users.json.
- You need to return a specific value from a JSON object by key.
- You want consistent API-safe error responses when configuration cannot be loaded.
- You are replacing ad-hoc JSON reads with a reusable helper.

## Inputs To Collect First
1. JSON file path.
2. Required key or key path (single key, nested path, or full object).
3. Missing-key behavior (raise error vs default value).
4. Caller context (internal helper, API route, startup bootstrap).
5. Expected data shape (list, dict, scalar).

## Procedure
1. Validate the target file name and path.
- Ensure the file ends with .json.
- Use a stable path strategy (project-root relative or absolute) and keep it consistent.

2. Open and parse JSON safely.
- Wrap file open and json.load in try/except.
- Catch OSError and json.JSONDecodeError.
- Convert failures to a clear error (for FastAPI callers, HTTPException status 500 with detail).

3. Resolve the requested value.
- For top-level lookup, use data.get(key, default).
- For nested lookup, walk a key path one segment at a time.
- If a required key is missing and no default is allowed, raise an explicit error.

4. Validate shape when required.
- If expecting list, verify isinstance(value, list).
- If expecting dict, verify isinstance(value, dict).
- Optionally normalize entries before returning (for example, trim strings or project to known fields).

5. Return deterministic output.
- Return the value in a stable shape, especially for API use.
- Avoid returning partially validated data.

## Decision Points
- Error surface:
Use HTTPException when errors should propagate through API responses. Use ValueError or RuntimeError for internal-only helpers.

- Missing key handling:
If the key is optional, use a default. If mandatory for app correctness, fail fast with a clear error.

- Normalization:
Normalize when data can be user-provided or mixed format (for example, list entries that may be str or dict).

## Completion Checklist
1. The helper only accepts .json paths.
2. File read and JSON decode errors are handled explicitly.
3. Missing-key behavior is defined and tested.
4. Returned value type matches caller expectations.
5. Error messages include file context and root cause.

## Output Template (Starter)
```python
import json
from fastapi import HTTPException


def read_json_value(file_path: str, key: str, default=None):
    if not file_path.endswith(".json"):
        raise HTTPException(status_code=500, detail=f"Expected .json file: {file_path}")

    try:
        with open(file_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read {file_path}: {exc}")

    if key in data:
        return data[key]
    if default is not None:
        return default
    raise HTTPException(status_code=500, detail=f"Missing required key '{key}' in {file_path}")
```

## In-Repo References
- Existing config loading and validation pattern: [config module](../../../app/core/config.py)