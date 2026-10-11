import json

import httpx
import pytest

from agent.actions import parse_action
from agent.config import Settings
from agent.vision import ModelMissing, OllamaVision, PlanContext, PlanFailed, VisionUnavailable


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def vis(handler, **kw):
    s = Settings(model="qwen3-vl:4b", model_retries=2, coord_mode="pixels", **kw)
    return OllamaVision(s, client(handler)), s


def reply(obj):
    return httpx.Response(200, json={"message": {"content": obj if isinstance(obj, str) else json.dumps(obj)}})


CTX = PlanContext(task="t", profile={"name": "A"})
V = lambda raw: parse_action(raw, 1280, 800, "pixels")


async def test_valid_reply_and_request_format():
    seen = {}

    def h(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return reply({"action": "click", "x": 10, "y": 20, "rationale": "r"})
    v, _ = vis(h)
    act, meta = await v.plan(b"\xff\xd8fake", CTX, V)
    assert act.action == "click" and meta["attempts"] == 1
    body = seen["body"]
    assert seen["url"].startswith("http://localhost:11434/")  # local only
    assert body["model"] == "qwen3-vl:4b" and body["stream"] is False and body["format"]["type"] == "object"
    assert len(body["messages"][1]["images"]) == 1 and body["options"]["temperature"] == 0


async def test_malformed_then_valid_recovers():
    calls = []

    def h(req):
        calls.append(1)
        return reply("not json at all") if len(calls) == 1 else reply({"action": "screenshot"})
    v, _ = vis(h)
    act, meta = await v.plan(b"x", CTX, V)
    assert act.action == "screenshot" and meta["attempts"] == 2 and meta["rejected"] == 1


async def test_retries_are_bounded():
    n = []
    v, s = vis(lambda r: (n.append(1), reply({"action": "dance"}))[1])
    with pytest.raises(PlanFailed) as e:
        await v.plan(b"x", CTX, V)
    assert len(n) == s.model_retries + 1 == e.value.attempts


async def test_server_down_raises_unavailable_and_health_reports():
    def h(req):
        raise httpx.ConnectError("refused")
    v, _ = vis(h)
    with pytest.raises(VisionUnavailable):
        await v.plan(b"x", CTX, V)
    hh = await v.health()
    assert hh["server"] is False and "ollama serve" in hh["message"]


async def test_model_missing():
    v, _ = vis(lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ModelMissing, match="ollama pull qwen3-vl:4b"):
        await v.plan(b"x", CTX, V)
    v2, _ = vis(lambda r: httpx.Response(200, json={"models": [{"name": "llama3:latest"}]}))
    h = await v2.health()
    assert h["server"] and not h["model_ready"] and "ollama pull qwen3-vl:4b" in h["message"]


async def test_model_ready_and_think_fallback():
    v, _ = vis(lambda r: httpx.Response(200, json={"models": [{"name": "qwen3-vl:4b"}]}))
    assert (await v.health())["model_ready"]
    bodies = []

    def h(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "think" in b:
            return httpx.Response(400, text='{"error":"\\"qwen3-vl:4b\\" does not support thinking"}')
        return reply({"action": "screenshot"})
    v, _ = vis(h)
    act, _ = await v.plan(b"x", CTX, V)
    assert act.action == "screenshot" and "think" not in bodies[-1]
