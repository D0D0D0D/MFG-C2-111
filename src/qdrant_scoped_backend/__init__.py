# PART: qdrant-scoped-backend v0.1.0 (parts@541a0e3)
"""Public API for the qdrant-scoped-backend common part."""

from .backend import (
    DEFAULT_METADATA_PASSTHROUGH,
    QdrantConnection,
    QdrantScopedBackend,
    build_scoped_tag,
    canonicalize_scope,
    derive_caller_id_from_source_id,
    fold_agent_name,
)

__all__ = [
    "DEFAULT_METADATA_PASSTHROUGH",
    "QdrantConnection",
    "QdrantScopedBackend",
    "build_scoped_tag",
    "canonicalize_scope",
    "derive_caller_id_from_source_id",
    "fold_agent_name",
]
