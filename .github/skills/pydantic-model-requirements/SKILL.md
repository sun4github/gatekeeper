---
name: pydantic-model-requirements
description: "Gather model requirements from the user and generate Pydantic models across one or more Python files with logical grouping, validation constraints, and deterministic naming. Use for schema scaffolding, request/response models, and model refactors in Python APIs."
argument-hint: "Describe the domain, model names, fields, validation rules, and preferred file organization."
user-invocable: true
---

# Pydantic Model Requirements

## What This Skill Produces
- A clarified model specification collected from the user before code generation.
- One or more Python schema files containing Pydantic models.
- Logical grouping of related models into the same file, with separation across files when domains differ.
- Deterministic model and field naming, with explicit `Field(...)` constraints when needed.
- Optional migration guidance when replacing existing ad-hoc dict payloads.

## Default Operating Profile
- Scope: workspace skill under `.github/skills/`.
- Pydantic target: v2 style for newly generated models.
- File grouping policy: balanced grouping (group request/response models for the same domain flow, split when concerns diverge).

## When To Use
- You need to create new Pydantic models from natural-language requirements.
- You need to split schema definitions into multiple files while keeping related models together.
- You need request/response models for FastAPI routes with clear validation.
- You need to normalize schema style across a Python codebase.

## Inputs To Collect First
1. Domain and use-case scope (for example: parental controls, billing, user profile).
2. Target model list and each model purpose (request, response, internal DTO, persistence shape).
3. Field-level specification per model:
- name
- type
- required vs optional
- default value
- constraints (for example: `min_length`, `ge`, `regex`, enum)
- example values when ambiguous
4. Relationship and reuse needs:
- inheritance or composition
- nested models
- shared mixins/base models
5. File organization requirements:
- preferred folder (for example `app/schemas/`)
- grouping rule for related models
- naming convention for file names
6. Compatibility constraints:
- Pydantic version assumptions
- backward compatibility requirements
- whether strict validation is required

## Procedure
1. Elicit missing requirements with focused questions.
- Ask only for unknowns that block deterministic code generation.
- If the user provides partial details, infer safe defaults and state assumptions.

2. Build a model-to-file plan before writing code.
- Group models into the same file only when they are logically connected by domain or endpoint flow.
- Split files when models belong to different bounded contexts.
- Keep filenames stable and explicit (for example `gatekeeper.py`, `auth.py`, `billing.py`).

3. Define model contracts.
- Prefer `BaseModel` plus `Field(...)` for validation constraints.
- Use concise docstrings for non-obvious models.
- Keep field names consistent with API payload contracts.
- Use `Optional[...]` only when the value is truly nullable or omitted.
- Default to Pydantic v2-compatible patterns unless the user asks for v1 compatibility.

4. Generate files in deterministic order.
- Create or update schema files with grouped models.
- Add imports once per file and avoid unused imports.
- Keep style consistent with existing schema conventions.

5. Validate integration impact.
- Confirm imports and references in route or service modules if needed.
- Check for duplicate model names across schema files.
- Ensure constraints match user requirements exactly.

6. Return an implementation summary plus follow-up questions.
- Summarize generated files and model groups.
- Highlight assumptions that should be confirmed.
- Ask the smallest set of ambiguity-resolution questions.

## Decision Points
- Single file vs multiple files:
Use one file when models share a domain and lifecycle. Use multiple files when the concerns are distinct or file size/readability degrades.

- Grouping strictness:
Use balanced grouping by default: colocate related request/response and mutation/query models for one domain flow, then split once cohesion drops.

- Shared base model:
Create a shared base only when at least two models benefit from common config or fields.

- Strict vs permissive fields:
If API safety is critical, prefer strict constraints. If backward compatibility is required, relax constraints and document tradeoffs.

## Completion Checklist
1. Every model has a clear purpose and deterministic name.
2. Every field has type, required/optional status, and constraints defined.
3. Models are grouped logically across files with no duplicated ownership.
4. Imports are minimal and valid; generated files are lint-clean.
5. Ambiguities are listed explicitly for user confirmation.

## In-Repo Reference Style
- Follow the schema style in [gatekeeper schema](../../../app/schemas/gatekeeper.py):
- Multiple related models in one file
- Concise model docstrings when context helps
- Validation constraints via `Field(...)`
