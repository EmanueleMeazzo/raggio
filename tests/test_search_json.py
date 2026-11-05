"""Native search JSON must preserve the ordinary FastAPI parsing contract."""
import asyncio
import json
import math
import random
import struct

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest

from raggio.app import SearchIn, _ValidatedJSONRequest, _ValidatedJSONRoute


def make_app(route_class):
    app = FastAPI()

    def auth(request: Request):
        if request.headers.get('x-api-key') != 'allowed':
            raise HTTPException(401, 'invalid API key')

    router = APIRouter(route_class=route_class)

    @router.post('/search', dependencies=[Depends(auth)])
    async def search(body: SearchIn):
        app.state.payload = body.model_dump()
        return {'accepted': True}

    app.include_router(router)
    return app


@pytest.fixture
def clients():
    with TestClient(make_app(APIRoute)) as baseline, TestClient(make_app(_ValidatedJSONRoute)) as native:
        yield baseline, native


def assert_same(left, right):
    assert type(left) is type(right)
    if isinstance(left, float):
        assert struct.pack('d', left) == struct.pack('d', right)
    elif isinstance(left, dict):
        assert left.keys() == right.keys()
        for key in left:
            assert_same(left[key], right[key])
    elif isinstance(left, list):
        assert len(left) == len(right)
        for a, b in zip(left, right):
            assert_same(a, b)
    else:
        assert left == right


PAYLOAD = {'query': {'vector': [0.25] * 8}, 'filter': {'doc_id': 'd'}}


def compare(clients, raw, key='allowed', content_type='application/json'):
    baseline, native = clients
    headers = {'x-api-key': key}
    if content_type is not None:
        headers['content-type'] = content_type
    expected = baseline.post('/search', content=raw, headers=headers)
    actual = native.post('/search', content=raw, headers=headers)
    assert (actual.status_code, actual.json()) == (expected.status_code, expected.json())
    if actual.status_code == 200:
        assert_same(native.app.state.payload, baseline.app.state.payload)


@pytest.mark.parametrize('payload', [
    PAYLOAD, {'query': {}},
    {'query': {'text': 'caffè Σ漢字', 'vector': ['0.25', True, -0.0]},
     'mode': 'hybrid', 'fusion': 'relative', 'alpha': '0.5', 'k': '5', 'nprobe': '16',
     'filter': {'doc_id': {'in': ['a', 'b']}, 'title': {'contains_any': ['Report', 'Σ']},
                'large': 2**100, 'fraction': 1.0000000000000002}},
    {'query': {'text': 'query'}, 'mode': 'text', 'scope': 'both'},
    {'query': {'vector': [0.25] * 8}, 'k': 1000,
     'expand': {'summary': True, 'siblings_topk': 3, 'siblings_all': True}, 'unknown': 'ignored'},
    {}, None, [], 'body', 1, {'query': None}, {'query': 'bad'}, {'query': []},
    {'query': {'text': 1}}, {'query': {'vector': ['bad']}},
    {'query': {}, 'mode': 'unknown'}, {'query': {}, 'filter': []},
    {'query': {}, 'k': 0}, {'query': {}, 'k': 1001}, {'query': {}, 'alpha': 1.5},
    {'query': {}, 'nprobe': 0}, {'query': {}, 'expand': {'siblings_topk': 0}},
])
@pytest.mark.parametrize('key', ['allowed', 'wrong'])
def test_models_errors_and_auth_order_match_fastapi(clients, payload, key):
    compare(clients, json.dumps(payload).encode(), key)


@pytest.mark.parametrize('raw', [
    b'', b'{', b'not json', b'{"query":', b'{"query":{}} trailing', b'\xff',
    b'{"query":{"text":"\\ud800"}}', b'{"query":{"text":"\\udc00"}}',
    b'{"query":{},"unknown":"\\ud800"}',
    b'{"query":{},"filter":{"title":"\\ud800"}}',
    b'{"query":{},"query":{"text":"last"}}',
    b'{"query":{},"filter":{"doc_id":"first","doc_id":"last"}}',
    b'\xef\xbb\xbf' + json.dumps(PAYLOAD).encode(),
    json.dumps(PAYLOAD).encode('utf-16'), json.dumps(PAYLOAD).encode('utf-32'),
    b'{"query":{},"filter":{"large":' + b'9' * 4301 + b'}}',
])
@pytest.mark.parametrize('key', ['allowed', 'wrong'])
def test_json_and_encoding_fallback_match_fastapi(clients, raw, key):
    compare(clients, raw, key)


@pytest.mark.parametrize('content_type', [None, 'text/plain', 'application/json',
    'application/vnd.raggio+json', 'application/json; charset=utf-8'])
def test_content_types_match_fastapi(clients, content_type):
    compare(clients, json.dumps(PAYLOAD).encode(), content_type=content_type)


def test_openapi_is_unchanged(clients):
    baseline, native = clients
    assert native.app.openapi() == baseline.app.openapi()


def test_cached_body_and_model_are_reused(monkeypatch):
    async def run():
        request = Request({'type': 'http'})
        request._body = json.dumps(PAYLOAD).encode()
        wrapped = _ValidatedJSONRequest(request, SearchIn)
        validate = SearchIn.model_validate_json
        calls = []
        def count(raw):
            calls.append(raw)
            return validate(raw)
        monkeypatch.setattr(SearchIn, 'model_validate_json', count)
        first = await wrapped.json()
        assert isinstance(first, SearchIn)
        assert await wrapped.json() is first
        assert await wrapped.body() is request._body
        assert calls == [request._body]
    asyncio.run(run())


def test_native_float_bits_and_filter_types_match_stdlib():
    rng = random.Random(1105)
    values = [struct.unpack('d', rng.randbytes(8))[0] for _ in range(10000)]
    values += [-0.0, 1.0000000000000002, 1e-308, 1e308, math.inf, -math.inf, math.nan]
    raw = json.dumps({'query': {'vector': values},
                      'filter': {'large': 2**100, 'values': [True, None, -0.0]}}).encode()
    assert_same(SearchIn.model_validate_json(raw).model_dump(),
                SearchIn.model_validate(json.loads(raw)).model_dump())
