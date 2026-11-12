import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from qraft import Engine
from qraft_server.app import create_app
from qraft_server.slack import SlackConfig, verify_signature

SEMANTIC = Path(__file__).resolve().parents[3] / "examples" / "ventes.semantic.yaml"
SECRET = "secret-de-test"


class FakeSlack:
    def __init__(self):
        self.sent = []

    def post_message(self, channel, text, blocks, thread_ts):
        self.sent.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts})


def signed(body: dict, secret: str = SECRET, ts: int | None = None):
    raw = json.dumps(body).encode()
    ts = str(ts or int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return raw, {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": "application/json"}


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    from qraft_eval.demo import build_demo_db

    path = build_demo_db(tmp_path_factory.mktemp("demo") / "ventes.duckdb")
    engine = Engine.from_url(f"duckdb:///{path.as_posix()}", semantic=SEMANTIC)
    slack = FakeSlack()
    client = TestClient(create_app(engine, slack=SlackConfig(signing_secret=SECRET, client=slack)))
    return client, slack


def test_signature_rules():
    raw, headers = signed({"a": 1})
    assert verify_signature(SECRET, headers["x-slack-request-timestamp"], raw, headers["x-slack-signature"])
    assert not verify_signature("autre", headers["x-slack-request-timestamp"], raw, headers["x-slack-signature"])
    old = str(int(time.time()) - 3600)
    assert not verify_signature(SECRET, old, raw, signed({"a": 1}, ts=int(old))[1]["x-slack-signature"])


def test_url_verification_and_bad_signature(setup):
    client, _ = setup
    raw, headers = signed({"type": "url_verification", "challenge": "abc"})
    assert client.post("/slack/events", content=raw, headers=headers).json() == {"challenge": "abc"}
    headers["x-slack-signature"] = "v0=faux"
    assert client.post("/slack/events", content=raw, headers=headers).status_code == 401


def test_mention_is_answered_in_thread(setup):
    client, slack = setup
    event = {"type": "event_callback", "event": {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "123.4",
                                                 "text": "<@UBOT> Chiffre d'affaires 2025 par région"}}
    raw, headers = signed(event)
    assert client.post("/slack/events", content=raw, headers=headers).status_code == 200
    message = slack.sent[-1]
    assert message["channel"] == "C1" and message["thread_ts"] == "123.4"
    assert "Chiffre d'affaires par région" in message["text"]
    assert "Île-de-France" in json.dumps(message["blocks"], ensure_ascii=False)


def test_bot_messages_and_retries_are_ignored(setup):
    client, slack = setup
    before = len(slack.sent)
    raw, headers = signed({"type": "event_callback", "event": {"type": "app_mention", "bot_id": "B1", "text": "x",
                                                              "channel": "C1", "ts": "1"}})
    client.post("/slack/events", content=raw, headers=headers)
    raw, headers = signed({"type": "event_callback", "event": {"type": "app_mention", "text": "<@U> CA 2025",
                                                              "channel": "C1", "ts": "2"}})
    headers["x-slack-retry-num"] = "1"
    client.post("/slack/events", content=raw, headers=headers)
    assert len(slack.sent) == before
