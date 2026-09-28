"""Shared S-3 detection for prohibited ECN decision/update content."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_EXACT_FORBIDDEN_PHRASES = frozenset(
    {
        "approval decision",
        "approve this",
        "approved for release",
        "regulatory compliant",
        "final regulatory compliance",
        "final external response",
        "承認可否",
        "承認します",
        "承認済み",
        "bomを更新",
        "avlを更新",
        "規制適合の最終",
        "規制適合の最終判断",
        "対外確定回答",
    }
)
_UPDATE_VERB = r"(?:updat|chang|revis|modif)\w*"
_BOM_AVL_UPDATE = re.compile(
    rf"\b(?:bom|avl)\b.*\b{_UPDATE_VERB}\b|\b{_UPDATE_VERB}\b.*\b(?:bom|avl)\b",
    re.IGNORECASE,
)


def contains_prohibited_content(value: Any) -> bool:
    """Return whether a value contains an operationally prohibited conclusion.

    BOM/AVL update instructions are intentionally detected in either English
    word order.  A bare BOM/AVL reference is not blocked: an action verb must
    occur in the same text value.
    """
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return any(phrase in normalized for phrase in _EXACT_FORBIDDEN_PHRASES) or bool(
            _BOM_AVL_UPDATE.search(normalized)
        )
    if isinstance(value, dict):
        return any(contains_prohibited_content(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_prohibited_content(item) for item in value)
    return False
