"""Keeping tool definitions and results small: every token here is paid on every tool call and every turn."""

from __future__ import annotations

import functools
import json


def compact(obj):
    """Drop null fields everywhere; results are for a model, so absent means unknown."""
    if isinstance(obj, dict):
        return {k: compact(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [compact(v) for v in obj]
    return obj


NOTE_HOOK = None  # set by the server: returns a short note to attach to a result now (see updates.py), or None


def lean_result(fn):
    """Tool results as minified JSON text: the SDK would pretty-print dicts (indent=2), costing ~30% more tokens."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if isinstance(result, dict) and NOTE_HOOK is not None:
            note = NOTE_HOOK()
            if note:
                result = {**result, "relaymcp_update": note}
        if isinstance(result, (dict, list)) and not is_content(result):
            return json.dumps(compact(result), separators=(",", ":"), ensure_ascii=False, default=str)
        return result
    return wrapper


def is_content(result) -> bool:
    """A list of ready-made MCP content blocks (image + text), which the SDK passes through as they are."""
    return isinstance(result, list) and bool(result) and all(hasattr(x, "type") and hasattr(x, "model_dump") for x in result)


def lean_schema(schema):
    """Drop what doesn't help a model pick arguments: pydantic's titles and `anyOf [X, null]` + `default: null` on
    optional parameters (optional is already expressed by not being required). Validation is unaffected."""
    if isinstance(schema, list):
        return [lean_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: lean_schema(v) for k, v in schema.items() if k != "title"}
    branches = out.get("anyOf")
    if isinstance(branches, list):
        real = [b for b in branches if b != {"type": "null"}]
        if len(real) == 1 and len(real) < len(branches):
            out.pop("anyOf")
            out = {**real[0], **out}
    if out.get("default", 0) is None:
        out.pop("default")
    return out
