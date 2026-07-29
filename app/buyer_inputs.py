"""Validation shared by every checkout rail for merchant-declared buyer inputs."""

from fastapi import HTTPException

from app import config
from app.models import Product


def declared_buyer_inputs(product: Product | None) -> list[dict]:
    """Return a product's valid buyer-input declarations in their safe wire shape."""
    raw = getattr(product, "buyer_inputs", None)
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "label": str(item.get("label") or name).strip(),
                "required": bool(item.get("required", True)),
            }
        )
    return out


def validate_buyer_inputs(product: Product | None, supplied: dict | None) -> dict:
    """Validate and normalize inputs before an order can be created or settled."""
    declared = declared_buyer_inputs(product)
    if not declared:
        return {}
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise HTTPException(422, "inputs must be an object")
    kept, missing = {}, []
    for field in declared:
        value = supplied.get(field["name"])
        value = value.strip() if isinstance(value, str) else value
        if value in (None, ""):
            if field["required"]:
                missing.append(field["name"])
            continue
        kept[field["name"]] = str(value)[: config.MAX_BUYER_INPUT_LEN]
    if missing:
        raise HTTPException(
            422,
            "this product requires buyer input before it can be sold: "
            + ", ".join(missing)
            + '. POST {"inputs": {"'
            + missing[0]
            + '": "…"}} with the payment.',
        )
    return kept
