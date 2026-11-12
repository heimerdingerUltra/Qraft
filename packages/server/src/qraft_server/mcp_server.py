"""Serveur MCP : les agents IA et les IDE interrogent Qraft avec les droits de l'utilisateur."""

from __future__ import annotations

import argparse
import os
from typing import Any

from qraft import Engine

from .store import AnswerStore, search_lexicon


class QraftTools:
    """Outils exposés en MCP ; testables sans transport."""

    def __init__(self, engine: Engine, store: AnswerStore | None = None) -> None:
        self.engine = engine
        self.store = store or AnswerStore()

    def ask(self, question: str, previous_id: str | None = None) -> dict[str, Any]:
        answer = self.engine.ask(question, previous=self.store.get(previous_id))
        self.store.put(answer)
        data = answer.to_dict()
        data["rows"] = data["rows"][:100]
        data.pop("chart", None)
        return data

    def search_knowledge(self, query: str) -> list[dict[str, Any]]:
        return search_lexicon(self.engine, query)

    def describe_metric(self, metric_id: str) -> dict[str, Any]:
        metric = self.engine.graph.semantic.metrics.get(metric_id)
        if metric is None:
            return {"error": f"métrique inconnue : {metric_id}",
                    "disponibles": sorted(self.engine.graph.semantic.metrics)}
        return {"id": metric.id, "label": metric.label, "description": metric.description, "unit": metric.unit,
                "synonyms": metric.synonyms, "expression": metric.expression, "filters": metric.filters,
                "certified": metric.certified}

    def get_result(self, answer_id: str, offset: int = 0, limit: int = 500) -> dict[str, Any]:
        answer = self.store.get(answer_id)
        if answer is None:
            return {"error": "réponse inconnue ou expirée"}
        data = answer.to_dict()
        return {"columns": data["columns"], "rows": data["rows"][offset:offset + limit], "total": len(data["rows"])}


def build_mcp(engine: Engine) -> Any:
    try:  # SDK MCP 2.x
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # SDK MCP 1.x
        from mcp.server.fastmcp import FastMCP as Server

    tools = QraftTools(engine)
    server = Server("qraft", instructions="Interroge les bases SQL de l'entreprise en français, avec vérification "
                                          "et score de confiance. Commence par search_knowledge en cas de doute "
                                          "sur le vocabulaire.")

    @server.tool(description="Pose une question en français sur les données ; renvoie interprétation, SQL, "
                             "résultat (100 premières lignes) et confiance.")
    def ask(question: str, previous_id: str | None = None) -> dict:
        return tools.ask(question, previous_id)

    @server.tool(description="Cherche une métrique, une dimension, une table ou une valeur dans le vocabulaire.")
    def search_knowledge(query: str) -> list[dict]:
        return tools.search_knowledge(query)

    @server.tool(description="Définition certifiée d'une métrique du glossaire.")
    def describe_metric(metric_id: str) -> dict:
        return tools.describe_metric(metric_id)

    @server.tool(description="Lignes d'un résultat déjà calculé, par pages.")
    def get_result(answer_id: str, offset: int = 0, limit: int = 500) -> dict:
        return tools.get_result(answer_id, offset, limit)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(prog="qraft-mcp", description="Serveur MCP de Qraft (transport stdio).")
    parser.add_argument("--db", default=os.environ.get("QRAFT_DB_URL"))
    parser.add_argument("--semantic")
    parser.add_argument("--pack")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    args = parser.parse_args()
    if not args.db:
        parser.error("indique la base avec --db ou QRAFT_DB_URL")
    build_mcp(Engine.from_url(args.db, semantic=args.semantic, pack=args.pack)).run(transport=args.transport)
