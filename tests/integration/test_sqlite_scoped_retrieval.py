"""R-1: real SQLite proves DB and KB tenant-scope filtering."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from shared.services.database.base_db import BaseDatabase
from src.nodes.precedent_recall_node import PrecedentRecallNode
from src.nodes.register_match_node import RegisterMatchNode
from src.services.service import ScopedVectorBackend


class SQLiteApprovedRegister(BaseDatabase):
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE approved_part_register ("
            "tenant_id TEXT NOT NULL, part_key TEXT NOT NULL, product_id TEXT NOT NULL, "
            "lot_id TEXT NOT NULL, register_id TEXT NOT NULL, "
            "PRIMARY KEY (tenant_id, part_key))"
        )
        self.connection.executemany(
            "INSERT INTO approved_part_register VALUES "
            "(:tenant_id, :part_key, :product_id, :lot_id, :register_id)",
            [
                {
                    "tenant_id": "tenant-a",
                    "part_key": "shared-part",
                    "product_id": "product-a",
                    "lot_id": "lot-a",
                    "register_id": "register-a",
                },
                {
                    "tenant_id": "tenant-b",
                    "part_key": "shared-part",
                    "product_id": "product-b",
                    "lot_id": "lot-b",
                    "register_id": "register-b",
                },
            ],
        )

    def fetch_one(self, query, params=None):
        row = self.connection.execute(query, params or {}).fetchone()
        return dict(row) if row is not None else None

    def fetch_many(self, query, params=None):
        rows = self.connection.execute(query, params or {}).fetchall()
        return [dict(row) for row in rows]

    def execute(self, query, params=None):
        self.connection.execute(query, params or {})


class SQLiteScopedStore:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE precedent ("
            "source_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "summary TEXT NOT NULL, category TEXT NOT NULL)"
        )
        self.connection.executemany(
            "INSERT INTO precedent VALUES (:source_id, :tenant_id, :summary, :category)",
            [
                {
                    "source_id": "kb_allowed",
                    "tenant_id": "tenant-a",
                    "summary": "material substitution observation",
                    "category": "performance",
                },
                {
                    "source_id": "kb_other_partition",
                    "tenant_id": "tenant-b",
                    "summary": "material substitution observation",
                    "category": "performance",
                },
            ],
        )

    def search_scoped(
        self, *, scope: Mapping[str, str], query: str, top_k: int
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT source_id, tenant_id, summary, category "
            "FROM precedent WHERE tenant_id = :tenant_id "
            "AND summary LIKE :query LIMIT :top_k",
            {
                "tenant_id": scope["tenant_id"],
                "query": f"%{query}%",
                "top_k": top_k,
            },
        ).fetchall()
        return [
            {
                "source_id": row["source_id"],
                "tenant_id": row["tenant_id"],
                "summary": row["summary"],
                "metadata": {"category": row["category"]},
            }
            for row in rows
        ]


def test_real_sqlite_register_lookup_hides_same_part_from_other_tenant() -> None:
    database = SQLiteApprovedRegister()
    envelope = json.dumps(
        {
            "tenant_id": "tenant-a",
            "normalized_parts": [{"part_key": "shared-part"}],
            "ecn": {"ecn_id": "ecn-a"},
        }
    )

    result = RegisterMatchNode(db=database).execute({"user_input": envelope})

    assert result["status"] == "success"
    assert result["match_ready"] is True
    assert result["impact_scope"]["matched_parts"] == [
        {
            "part_key": "shared-part",
            "product_id": "product-a",
            "lot_id": "lot-a",
            "source_id": "register-a",
        }
    ]
    assert result["impact_scope"]["search_scope"] == {"tenant_id": "tenant-a"}


def test_real_sqlite_returns_one_stored_scope_hit_and_hides_other_partition() -> None:
    store = SQLiteScopedStore()
    node = PrecedentRecallNode(backend=ScopedVectorBackend(store))

    result = node.execute(
        {
            "tenant_id": "tenant-a",
            "ecn": {"body": "material substitution observation"},
            "register_records": [],
        }
    )

    assert result["status"] == "success"
    assert [record["source_id"] for record in result["retrieved_records"]] == ["kb_allowed"]
    assert result["retrieved_records"][0]["metadata"] | {
        "tenant_id": "tenant-a",
    } == result["retrieved_records"][0]["metadata"]
    assert result["retrieval_rejected"] == []
