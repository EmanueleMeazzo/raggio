"""Native ingest parsing must retain the ordinary FastAPI request contract."""
import asyncio
import json
import math
import random
import struct

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest

from raggio.app import IngestIn, _IngestJSONRequest, _IngestJSONRoute


def make_app(route_class):
    app = FastAPI()

    def auth(request: Request):
        if request.headers.get("x-api-key") != "allowed":
            raise HTTPException(401, "invalid API key")

    router = APIRouter(route_class=route_class)

    @router.post("/documents", status_code=202, dependencies=[Depends(auth)])
    async def ingest(body: IngestIn):
        app.state.payload = body.model_dump()
        return {"accepted": True}

    app.include_router(router)
    return app


PAYLOAD = {"documents": [{"doc_id": "d", "chunks": [{
    "id": "c", "text": "retrieval data", "vector": [0.25] * 8,
    "position": 0, "metadata": {"source": "probe"},
}]}]}


@pytest.fixture
def clients():
    with TestClient(make_app(APIRoute)) as baseline, TestClient(make_app(_IngestJSONRoute)) as native:
        yield baseline, native


def assert_same(a, b):
    """Include float bits (NaN and signed zero), not only approximate equality."""
    assert type(a) is type(b)
    if isinstance(a, float):
        assert struct.pack("d", a) == struct.pack("d", b)
    elif isinstance(a, dict):
        assert a.keys() == b.keys()
        for key in a:
            assert_same(a[key], b[key])
    elif isinstance(a, list):
        assert len(a) == len(b)
        for left, right in zip(a, b):
            assert_same(left, right)
    else:
        assert a == b


@pytest.mark.parametrize("payload", [
    PAYLOAD,
    {"documents": [{"doc_id": "d"}]},
    {"documents": [{"doc_id": "d", "summary": {"text": "summary"}}]},
    {"documents": [{"doc_id": "d", "chunks": [{
        "id": "c", "text": "caffè Σ漢字", "position": "2", "vector": ["0.25", True],
        "metadata": {"large": 2**100, "fraction": 1.0000000000000002, "zero": -0.0},
    }]}]},
    {}, {"documents": []}, {"documents": None}, {"documents": [1]},
    {"documents": [{}]}, {"documents": [{"doc_id": 1}]},
    {"documents": [{"doc_id": "d", "chunks": "bad"}]},
    {"documents": [{"doc_id": "d", "chunks": [{"id": "c", "vector": ["bad"]}]}]},
    {"documents": [{"doc_id": "d", "chunks": [{"id": "c", "position": 1.5}]}]},
    {"documents": [{"doc_id": "d", "chunks": [{"id": "c", "metadata": []}]}]},
    None, [], "body", 1,
])
@pytest.mark.parametrize("key", ["allowed", "wrong"])
def test_models_errors_and_auth_order_match_fastapi(clients, payload, key):
    baseline, native = clients
    kwargs = {"content": json.dumps(payload), "headers": {
        "content-type": "application/json", "x-api-key": key,
    }}
    expected = baseline.post("/documents", **kwargs)
    actual = native.post("/documents", **kwargs)
    assert (actual.status_code, actual.json()) == (expected.status_code, expected.json())
    if actual.status_code == 202:
        assert_same(native.app.state.payload, baseline.app.state.payload)


@pytest.mark.parametrize("body", [
    b"", b"{", b"not json", b'{"documents":', b'{"documents":[]} trailing',
    b'{"documents":[{"doc_id":"d","chunks":[{"id":"c","text":"\\ud800"}]}]}',
    b'{"documents":[{"doc_id":"d","chunks":[{"id":"c","text":"\\udc00"}]}]}',
    b'{"documents":[{"doc_id":"d","unknown":"\\ud800"}]}',
    b'{"documents":[],"documents":[{"doc_id":"d"}]}',
    b'\xff',
    b'\xef\xbb\xbf' + json.dumps(PAYLOAD).encode(),
    json.dumps(PAYLOAD).encode("utf-16"),
    json.dumps(PAYLOAD).encode("utf-32"),
])
@pytest.mark.parametrize("key", ["allowed", "wrong"])
def test_json_and_encoding_fallback_matches_fastapi(clients, body, key):
    baseline, native = clients
    kwargs = {"content": body, "headers": {"content-type": "application/json", "x-api-key": key}}
    expected = baseline.post("/documents", **kwargs)
    actual = native.post("/documents", **kwargs)
    assert (actual.status_code, actual.json()) == (expected.status_code, expected.json())
    if actual.status_code == 202:
        assert_same(native.app.state.payload, baseline.app.state.payload)


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/json", "application/vnd.raggio+json", "application/json; charset=utf-8"])
def test_content_type_behavior_matches_fastapi(clients, content_type):
    baseline, native = clients
    headers = {"x-api-key": "allowed"}
    if content_type:
        headers["content-type"] = content_type
    kwargs = {"content": json.dumps(PAYLOAD).encode(), "headers": headers}
    expected = baseline.post("/documents", **kwargs)
    actual = native.post("/documents", **kwargs)
    assert (actual.status_code, actual.json()) == (expected.status_code, expected.json())


def test_openapi_is_unchanged(clients):
    baseline, native = clients
    assert native.app.openapi() == baseline.app.openapi()


def test_request_reuses_cached_body_and_parses_once(monkeypatch):
    async def run():
        request = Request({"type": "http"})
        request._body = json.dumps(PAYLOAD).encode()
        native = _IngestJSONRequest(request)
        original = IngestIn.model_validate_json
        calls = []

        def validate(data):
            calls.append(data)
            return original(data)

        monkeypatch.setattr(IngestIn, "model_validate_json", validate)
        first = await native.json()
        assert isinstance(first, IngestIn)
        assert await native.json() is first
        assert await native.body() is request._body
        assert calls == [request._body]

    asyncio.run(run())


def test_native_numbers_match_stdlib_bits():
    rng = random.Random(719)
    values = [struct.unpack("d", rng.randbytes(8))[0] for _ in range(10000)]
    # NaN payloads are canonicalized by JSON itself; compare against json.loads.
    values += [-0.0, 1.0000000000000002, 1e-308, 1e308, math.inf, -math.inf, math.nan]
    raw = json.dumps({"documents": [{"doc_id": "d", "chunks": [{
        "id": "c", "vector": values, "metadata": {"large": 2**100},
    }]}]}).encode()
    expected = IngestIn.model_validate(json.loads(raw)).model_dump()
    actual = IngestIn.model_validate_json(raw).model_dump()
    assert_same(actual, expected)


def test_integer_digit_limit_matches_fastapi(clients):
    baseline, native = clients
    raw = (b'{"documents":[{"doc_id":"d","chunks":[{"id":"c","metadata":{"large":'
           + b"9" * 4301 + b'}}]}]}')
    kwargs = {"content": raw, "headers": {"content-type": "application/json", "x-api-key": "allowed"}}
    expected = baseline.post("/documents", **kwargs)
    actual = native.post("/documents", **kwargs)
    assert (actual.status_code, actual.json()) == (expected.status_code, expected.json())
