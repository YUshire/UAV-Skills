"""Validate the target descriptions attached to a planning card."""
import re


def validate_targets(targets):
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
        raise ValueError("targets must contain 1..64 category/description entries")
    result, seen = [], set()
    for item in targets:
        if not isinstance(item, dict) or set(item) != {"target_id", "category", "description"}:
            raise ValueError("each target requires target_id, category and description")
        tid, category, description = item["target_id"], item["category"], item["description"]
        if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tid) or tid in seen:
            raise ValueError("target_id must be unique and contain 1..80 letters/digits/_/-")
        if not isinstance(category, str) or not category.strip() or len(category) > 100:
            raise ValueError("category must contain 1..100 characters")
        if not isinstance(description, str) or not description.strip() or len(description) > 2000:
            raise ValueError("description must contain 1..2000 characters")
        seen.add(tid)
        result.append(dict(target_id=tid, category=category.strip().lower(), description=description.strip()))
    return result
