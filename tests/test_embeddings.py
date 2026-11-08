import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import raggio.embeddings as embeddings
from raggio.embeddings import Embedder


@pytest.fixture
def backoffs(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    # Patch only the embedder's asyncio reference; other coroutines keep real scheduling.
    monkeypatch.setattr(embeddings, "asyncio", SimpleNamespace(sleep=sleep))
    return delays


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 408, 422])
def test_permanent_http_errors_fail_without_retry(status, backoffs):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "bad request"})

    async def run():
        emb = Embedder("https://embedding.test/v1", "", "test-model")
        await emb._client.aclose()
        emb._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(httpx.HTTPStatusError) as info:
                await emb.embed(["text"])
            assert info.value.response.status_code == status
        finally:
            await emb.aclose()

    asyncio.run(run())
    assert len(requests) == 1
    assert backoffs == []


@pytest.mark.parametrize("failure", [429, 500, 502, 503, 599,
                                   httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transient_errors_retry_and_preserve_payload_and_result_order(failure, backoffs):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) < 3:
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure("temporary failure", request=request)
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]})

    async def run():
        emb = Embedder("https://embedding.test/v1", "", "test-model")
        await emb._client.aclose()
        emb._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await emb.embed(["one", "two"]) == [[1.0, 0.0], [0.0, 1.0]]
        finally:
            await emb.aclose()

    asyncio.run(run())
    assert requests == [{"input": ["one", "two"], "model": "test-model"}] * 3
    assert backoffs == [1, 2]


@pytest.mark.parametrize("failure", [429, 503, httpx.ConnectError])
def test_retry_exhaustion_does_not_sleep_after_the_last_attempt(failure, backoffs):
    requests = []

    def respond(request):
        requests.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("temporary failure", request=request)

    async def run():
        emb = Embedder("https://embedding.test/v1", "", "")
        await emb._client.aclose()
        emb._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(RuntimeError, match="failed after 3 attempts") as info:
                await emb.embed(["text"])
            assert isinstance(info.value.__cause__, httpx.HTTPError)
        finally:
            await emb.aclose()

    asyncio.run(run())
    assert len(requests) == 3
    assert backoffs == [1, 2]


def test_closed_client_fails_without_retry(backoffs):
    async def run():
        emb = Embedder("https://embedding.test/v1", "", "")
        await emb.aclose()
        with pytest.raises(RuntimeError, match="client has been closed"):
            await emb.embed(["text"])

    asyncio.run(run())
    assert backoffs == []


def test_transport_error_from_a_closed_in_flight_client_is_not_retried(backoffs):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def respond(request):
            entered.set()
            await release.wait()
            raise httpx.ReadError("connection closed during request", request=request)

        emb = Embedder("https://embedding.test/v1", "", "")
        await emb._client.aclose()
        emb._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        request = asyncio.create_task(emb.embed(["text"]))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await emb.aclose()
        finally:
            release.set()
        with pytest.raises(httpx.ReadError):
            await request

    asyncio.run(run())
    assert backoffs == []


@pytest.mark.parametrize("response", [httpx.Response(200, content=b"invalid JSON"),
                                     httpx.Response(200, json={"wrong": []})])
def test_bad_embedding_response_fails_without_retry(response, backoffs):
    requests = []

    def respond(request):
        requests.append(request)
        return response

    async def run():
        emb = Embedder("https://embedding.test/v1", "", "")
        await emb._client.aclose()
        emb._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises((ValueError, KeyError)):
                await emb.embed(["text"])
        finally:
            await emb.aclose()

    asyncio.run(run())
    assert len(requests) == 1
    assert backoffs == []
