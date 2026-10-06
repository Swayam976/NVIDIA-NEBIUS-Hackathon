"""Aggregates every skill's JSON schema (for the model) and Python
implementation (for us to actually run) into two flat structures the
agent loop can use.
"""

from __future__ import annotations

from . import debugging, docs, module_modifier, project_state, verification, workflow

_MODULES = [project_state, verification, module_modifier, docs, workflow, debugging]

TOOL_SCHEMAS: list[dict] = [schema for mod in _MODULES for schema in mod.SCHEMAS]
TOOL_IMPLS: dict = {name: fn for mod in _MODULES for name, fn in mod.IMPLS.items()}

assert len(TOOL_SCHEMAS) == len(TOOL_IMPLS), "every schema needs exactly one matching impl"
