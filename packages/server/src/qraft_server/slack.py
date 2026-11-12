"""Bot Slack (Events API) : une mention ou un message direct -> réponse Qraft dans le fil.

Sécurité : chaque requête est authentifiée par la signature Slack (HMAC SHA-256 du corps
avec le secret de signature) et refusée si elle a plus de 5 minutes. La réponse HTTP part
tout de suite ; la question est traitée en tâche de fond (Slack exige moins de 3 s).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from qraft import Answer, Engine, Identity

_MENTION = re.compile(r"<@[A-Z0-9]+>")


class SlackClient(Protocol):
    def post_message(self, channel: str, text: str, blocks: list[dict[str, Any]], thread_ts: str | None) -> None: ...


class HttpSlackClient:
    """Client minimal de l'API Web Slack (chat.postMessage)."""

    def __init__(self, bot_token: str, http: Any | None = None) -> None:
        if http is None:
            import httpx

            http = httpx.Client(base_url="https://slack.com/api", timeout=10.0)
        self.http = http
        self.headers = {"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8"}

    def post_message(self, channel: str, text: str, blocks: list[dict[str, Any]], thread_ts: str | None) -> None:
        payload = {"channel": channel, "text": text, "blocks": blocks}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        self.http.post("/chat.postMessage", headers=self.headers, json=payload)


@dataclass
class SlackConfig:
    signing_secret: str
    client: SlackClient
    max_age_s: int = 300


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str, *, max_age_s: int = 300,
                     now: float | None = None) -> bool:
    try:
        age = abs((now or time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > max_age_s:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def render_blocks(answer: Answer, max_rows: int = 10) -> tuple[str, list[dict[str, Any]]]:
    """Message Slack : interprétation, aperçu du résultat, confiance."""
    if answer.status in ("abstention", "erreur", "clarification"):
        text = answer.message or "Pas de réponse fiable."
        blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        if answer.alternatives:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                       "text": "\n".join(f"• {a}" for a in answer.alternatives)}})
        return text, blocks
    data = answer.to_dict()
    lines = [" | ".join(data["columns"])]
    for row in data["rows"][:max_rows]:
        lines.append(" | ".join("" if v is None else (f"{v:,.2f}".replace(",", " ") if isinstance(v, float) else str(v))
                                for v in row))
    if len(data["rows"]) > max_rows:
        lines.append(f"… {len(data['rows']) - max_rows} lignes de plus")
    confidence = data["confidence"] or {}
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{answer.interpretation}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "```" + "\n".join(lines) + "```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Confiance : {confidence.get('tier', '?')} · " + " · ".join(answer.warnings[:2])}]},
    ]
    return answer.interpretation, blocks


def slack_endpoint(engine: Engine, config: SlackConfig):
    async def events(request: Request) -> Response:
        body = await request.body()
        if not verify_signature(config.signing_secret, request.headers.get("x-slack-request-timestamp", ""), body,
                                request.headers.get("x-slack-signature", ""), max_age_s=config.max_age_s):
            return PlainTextResponse("signature invalide", status_code=401)
        payload = json.loads(body or b"{}")
        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": payload.get("challenge")})
        if request.headers.get("x-slack-retry-num"):
            return PlainTextResponse("déjà reçu")  # Slack réessaie : on a déjà traité l'événement
        event = payload.get("event") or {}
        if event.get("bot_id") or event.get("type") not in ("app_mention", "message"):
            return PlainTextResponse("ignoré")
        if event.get("type") == "message" and event.get("channel_type") != "im":
            return PlainTextResponse("ignoré")
        question = _MENTION.sub("", event.get("text", "")).strip()
        if not question:
            return PlainTextResponse("vide")

        def answer_in_thread() -> None:
            answer = engine.ask(question, identity=Identity(user=f"slack:{event.get('user', 'inconnu')}"))
            text, blocks = render_blocks(answer)
            config.client.post_message(event["channel"], text, blocks, event.get("thread_ts") or event.get("ts"))

        return PlainTextResponse("ok", background=BackgroundTask(answer_in_thread))

    return events
