"""Bridge between process_catalog step inputs and collect_user_input fields.

Converts the ``inputs`` dict from a process step YAML into the field array
expected by the ``collect_user_input`` MCP tool.

Usage::

    from src.shared.interaction.catalog_bridge import catalog_inputs_to_fields

    fields = catalog_inputs_to_fields(step["inputs"])
    # → [{"id": "nome", "field_type": "text", "label": "Nome", ...}, ...]
"""

from __future__ import annotations


def catalog_inputs_to_fields(inputs: dict) -> list[dict]:
    """Convert process_catalog step inputs to collect_user_input fields.

    Args:
        inputs: The ``inputs`` dict from a process step YAML
                (e.g. ``{"required": [...], "optional": [...]}``).

    Returns:
        List of field dicts ready for ``collect_user_input`` ``fields`` param.
    """
    fields: list[dict] = []
    for section in ("required", "optional"):
        for inp in inputs.get(section, []):
            field = _convert_single_input(inp)
            if section == "required":
                field["required"] = True
            fields.append(field)
    return fields


# ── Internal helpers ─────────────────────────────────────────────────────

_TYPE_MAP: dict[str, str] = {
    "text": "text",
    "number": "number",
    "integer": "number",
    "boolean": "checkbox",
    "date": "date",
    "email": "text",
    "enum": "select",
}


def _convert_single_input(inp: dict) -> dict:
    """Convert a single catalog input to an interaction field."""
    catalog_type = inp.get("type", "text")
    ft = _TYPE_MAP.get(catalog_type, "text")
    validation = inp.get("validation", {})

    # ── Heuristics ───────────────────────────────────────────────────
    if ft == "text" and validation.get("max_length", 0) > 200:
        ft = "textarea"
    if catalog_type == "enum" and len(inp.get("enum_values", [])) <= 4:
        ft = "radio"

    field: dict = {
        "id": inp["name"],
        "field_type": ft,
        "label": inp.get("label", _name_to_label(inp.get("name", ""))),
    }

    if inp.get("description"):
        field["description"] = inp["description"]

    # ── Validation / constraints ─────────────────────────────────────
    if ft == "number":
        if "min" in validation:
            field["min"] = validation["min"]
        if "max" in validation:
            field["max"] = validation["max"]
        if catalog_type == "integer":
            field["step"] = 1
    if ft in ("text", "textarea"):
        if "min_length" in validation:
            field["min_length"] = validation["min_length"]
        if "max_length" in validation:
            field["max_length"] = validation["max_length"]
    if ft == "textarea":
        field["rows"] = min(validation.get("max_length", 5000) // 200, 20) or 6
    if catalog_type == "email":
        field["validation"] = {"pattern": "email"}
    if catalog_type == "enum":
        values = inp.get("enum_values", [])
        field["options"] = [
            {"value": v, "label": _name_to_label(v)} for v in values
        ]

    return field


def _name_to_label(name: str) -> str:
    """Convert a snake_case name to a Title Case label."""
    return name.replace("_", " ").title()
