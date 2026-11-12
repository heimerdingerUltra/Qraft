"""API REST + SSE au-dessus du moteur (Starlette, servie par uvicorn)."""

from __future__ import annotations

import json
from typing import Any

import anyio
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from qraft import Engine, Identity, __version__

from .store import AnswerStore, search_lexicon

ROWS_PER_EVENT = 500


def _identity(request: Request, trust_headers: bool, default: Identity) -> Identity:
    """Identité de l'appelant. Les en-têtes ne sont crus que derrière une passerelle qui authentifie."""
    if not trust_headers:
        return default
    user = request.headers.get("x-qraft-user")
    if not user:
        return default
    try:
        attributes = json.loads(request.headers.get("x-qraft-attributes", "{}"))
    except json.JSONDecodeError:
        attributes = {}
    return Identity(user=user, attributes={str(k): str(v) for k, v in attributes.items()})


def create_app(engine: Engine, *, trust_identity_headers: bool = False, store: AnswerStore | None = None) -> Starlette:
    answers = store or AnswerStore()

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__, "graph_version": engine.graph.version,
                             "dialect": engine.graph.catalog.dialect, "tables": len(engine.graph.catalog.tables)})

    async def ask(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "corps JSON invalide"}, status_code=400)
        question = str(body.get("question", "")).strip()
        if not question:
            return JSONResponse({"error": "champ « question » manquant"}, status_code=400)
        previous = answers.get(body.get("previous_id"))
        identity = _identity(request, trust_identity_headers, engine.identity)
        answer = await anyio.to_thread.run_sync(
            lambda: engine.ask(question, previous=previous, execute=bool(body.get("execute", True)), identity=identity))
        answers.put(answer)
        return JSONResponse(answer.to_dict())

    async def stream(request: Request) -> Response:
        question = request.query_params.get("q", "").strip()
        if not question:
            return JSONResponse({"error": "paramètre q manquant"}, status_code=400)
        previous = answers.get(request.query_params.get("previous_id"))
        identity = _identity(request, trust_identity_headers, engine.identity)

        async def events():
            plan = await anyio.to_thread.run_sync(
                lambda: engine.ask(question, previous=previous, execute=False, identity=identity))
            if plan.status in ("abstention", "clarification", "erreur"):
                yield {"event": plan.status, "data": json.dumps(plan.to_dict(), ensure_ascii=False)}
                return
            yield {"event": "interpretation", "data": json.dumps(
                {"text": plan.interpretation, "intent": plan.intent.to_dict() if plan.intent else None,
                 "assumptions": plan.assumptions, "rules": plan.rules}, ensure_ascii=False)}
            yield {"event": "sql", "data": json.dumps({"sql": plan.sql}, ensure_ascii=False)}
            answer = await anyio.to_thread.run_sync(
                lambda: engine.ask(question, previous=previous, identity=identity))
            answers.put(answer)
            data = answer.to_dict()
            if answer.status == "erreur":
                yield {"event": "erreur", "data": json.dumps(data, ensure_ascii=False)}
                return
            for start in range(0, max(len(data["rows"]), 1), ROWS_PER_EVENT):
                yield {"event": "rows", "data": json.dumps(
                    {"columns": data["columns"], "rows": data["rows"][start:start + ROWS_PER_EVENT], "offset": start},
                    ensure_ascii=False)}
            if data["chart"]:
                yield {"event": "chart", "data": json.dumps(data["chart"], ensure_ascii=False)}
            yield {"event": "confidence", "data": json.dumps(data["confidence"], ensure_ascii=False)}
            yield {"event": "done", "data": json.dumps({"id": answer.id, "status": answer.status,
                                                        "warnings": answer.warnings}, ensure_ascii=False)}

        return EventSourceResponse(events())

    async def feedback(request: Request) -> JSONResponse:
        answer_id = request.path_params["answer_id"]
        if answers.get(answer_id) is None:
            return JSONResponse({"error": "réponse inconnue"}, status_code=404)
        body = await request.json()
        rating = int(body.get("rating", 0))
        if rating not in (-1, 1):
            return JSONResponse({"error": "rating doit valoir -1 ou 1"}, status_code=400)
        engine.feedback(answer_id, rating=rating, comment=body.get("comment"),
                        identity=_identity(request, trust_identity_headers, engine.identity))
        return JSONResponse({"status": "enregistré"})

    async def result(request: Request) -> Response:
        answer = answers.get(request.path_params["answer_id"])
        if answer is None:
            return JSONResponse({"error": "réponse inconnue"}, status_code=404)
        if request.query_params.get("format") == "arrow":
            import pyarrow as pa

            table = answer.to_arrow()
            sink = pa.BufferOutputStream()
            with pa.ipc.new_stream(sink, table.schema) as writer:
                writer.write_table(table)
            return Response(sink.getvalue().to_pybytes(), media_type="application/vnd.apache.arrow.stream")
        data = answer.to_dict()
        return JSONResponse({"columns": data["columns"], "rows": data["rows"]})

    async def knowledge_search(request: Request) -> JSONResponse:
        return JSONResponse({"results": search_lexicon(engine, request.query_params.get("q", ""))})

    async def knowledge_metrics(request: Request) -> JSONResponse:
        metrics: list[dict[str, Any]] = [
            {"id": m.id, "label": m.label or m.id, "description": m.description, "unit": m.unit,
             "synonyms": m.synonyms, "certified": m.certified}
            for m in sorted(engine.graph.semantic.metrics.values(), key=lambda m: m.id)]
        return JSONResponse({"metrics": metrics})

    return Starlette(routes=[
        Route("/v1/health", health),
        Route("/v1/questions", ask, methods=["POST"]),
        Route("/v1/questions/stream", stream),
        Route("/v1/questions/{answer_id}/feedback", feedback, methods=["POST"]),
        Route("/v1/results/{answer_id}", result),
        Route("/v1/knowledge/search", knowledge_search),
        Route("/v1/knowledge/metrics", knowledge_metrics),
    ])


def run(engine: Engine, *, host: str = "127.0.0.1", port: int = 8080, trust_identity_headers: bool = False) -> None:
    import uvicorn

    uvicorn.run(create_app(engine, trust_identity_headers=trust_identity_headers), host=host, port=port)
