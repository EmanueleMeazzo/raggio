"""The bench harness's reuse-or-ingest decision (spec §5 finding 3): an engine that
doesn't answer, a chunk count that doesn't match, or a fingerprint that doesn't match
must abort the run, never fall through to the DELETE + 31-minute re-ingest path."""
import asyncio
import json

import httpx
import pytest

import sys
sys.path.insert(0, "bench")
from bench_reuse import (STATE_TIMEOUT_S, BenchAbort, EngineState, decide, fingerprint_path,
                         prepare_engine, raggio_state, read_fingerprint, wait_idle,
                         weaviate_state, write_fingerprint)

BASE = "http://engine"
FP = {"limit": 2549619, "seed": 42, "queries": 500, "text_v": "real abstracts, m @abcd1234/d1024"}
EXPECTED = 2549619 - 500


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=STATE_TIMEOUT_S)


def raggio_handler(info=None, info_status=200, names=("bench",), list_status=200):
    def handler(request):
        if request.url.path == "/collections":
            return httpx.Response(list_status, json={"collections": list(names)})
        if request.url.path == "/collections/bench":
            if info is None:
                return httpx.Response(info_status, json={"detail": "collection 'bench' not found"})
            return httpx.Response(info_status, json=info)
        return httpx.Response(500)
    return handler


def state_of(handler):
    async def go():
        async with client_for(handler) as c:
            return await raggio_state(c, BASE, "bench")
    return asyncio.run(go())


def test_state_timeout_covers_a_cold_collection_open():
    # the first GET after a container start builds the collection on the event loop
    # (128 s on the DGX at 2.5M rows); the old 30 s read that as "no data" and re-ingested
    assert STATE_TIMEOUT_S >= 900


def test_raggio_state_parses_count_index_and_pending():
    info = {"chunks": 12, "pending_jobs": 3, "index": {"type": "ivf", "nlist": 4, "nprobe": 2}}
    assert state_of(raggio_handler(info)) == EngineState(chunks=12, ivf=True, pending_jobs=3)
    flat = {"chunks": 12, "pending_jobs": 0, "index": {"type": "flat"}}
    assert state_of(raggio_handler(flat)) == EngineState(chunks=12, ivf=False, pending_jobs=0)


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_unreachable_raggio_aborts_instead_of_reporting_no_data(exc):
    def handler(request):
        raise exc("engine not answering", request=request)
    with pytest.raises(BenchAbort, match="--state-timeout"):
        state_of(handler)


def test_404_confirmed_by_the_collection_list_means_absent():
    assert state_of(raggio_handler(None, 404, names=("other",))) == EngineState(chunks=None)


@pytest.mark.parametrize("names,list_status", [(("bench",), 200), ((), 401), ((), 500)])
def test_404_the_collection_list_does_not_confirm_aborts(names, list_status):
    with pytest.raises(BenchAbort, match="does not confirm"):
        state_of(raggio_handler(None, 404, names=names, list_status=list_status))


def test_error_status_aborts():
    with pytest.raises(BenchAbort, match="HTTP 503"):
        state_of(raggio_handler({"detail": "busy"}, 503))


def test_unexpected_body_aborts():
    with pytest.raises(BenchAbort, match="unexpected body"):
        state_of(raggio_handler({"name": "bench"}))


def test_wait_idle_polls_until_the_job_queue_drains():
    pending = iter([3, 1, 0])

    def handler(request):
        return httpx.Response(200, json={"chunks": 5, "pending_jobs": next(pending),
                                         "index": {"type": "flat"}})

    async def go():
        async with client_for(handler) as c:
            return await wait_idle(c, BASE, "bench", poll_s=0)
    assert asyncio.run(go()) == EngineState(chunks=5, pending_jobs=0)


def test_wait_idle_gives_up_loudly():
    def handler(request):
        return httpx.Response(200, json={"chunks": 5, "pending_jobs": 2, "index": {"type": "flat"}})

    async def go():
        async with client_for(handler) as c:
            return await wait_idle(c, BASE, "bench", poll_s=0, max_wait_s=0)
    with pytest.raises(BenchAbort, match="2 jobs still pending"):
        asyncio.run(go())


def test_wait_idle_returns_at_once_for_an_absent_collection():
    async def go():
        async with client_for(raggio_handler(None, 404, names=())) as c:
            return await wait_idle(c, BASE, "bench", poll_s=0, max_wait_s=0)
    assert asyncio.run(go()) == EngineState(chunks=None)


def weaviate_of(handler):
    async def go():
        async with client_for(handler) as c:
            return await weaviate_state(c, BASE, "Email")
    return asyncio.run(go())


def test_weaviate_state_counts_and_absent_class():
    def present(request):
        if request.url.path == "/v1/schema/Email":
            return httpx.Response(200, json={"class": "Email"})
        return httpx.Response(200, json={"data": {"Aggregate": {"Email": [{"meta": {"count": 7}}]}}})
    assert weaviate_of(present) == EngineState(chunks=7)
    assert weaviate_of(lambda request: httpx.Response(404)) == EngineState(chunks=None)


def test_weaviate_graphql_error_with_http_200_aborts():
    def handler(request):
        if request.url.path == "/v1/schema/Email":
            return httpx.Response(200, json={"class": "Email"})
        return httpx.Response(200, json={"errors": [{"message": "shard not ready"}]})
    with pytest.raises(BenchAbort, match="shard not ready"):
        weaviate_of(handler)


def test_weaviate_unreachable_aborts():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    with pytest.raises(BenchAbort, match="unreachable"):
        weaviate_of(handler)


def test_fingerprint_path_is_keyed_by_engine_data_and_limit(tmp_path):
    assert fingerprint_path(tmp_path, "raggio", 2549619) == tmp_path / "fingerprint-raggio-2549619.json"
    assert fingerprint_path(tmp_path, "raggio", 553015) != fingerprint_path(tmp_path, "raggio", 2549619)


def test_fingerprint_roundtrip_is_atomic(tmp_path):
    path = fingerprint_path(tmp_path / "state", "raggio", 2549619)  # directory created on write
    assert read_fingerprint(path) is None
    write_fingerprint(path, FP)
    assert read_fingerprint(path) == FP
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind


@pytest.mark.parametrize("content", ["", '{"limit": 25496', "[1, 2]", "null"])
def test_unreadable_fingerprint_never_matches(tmp_path, content):
    path = tmp_path / "fingerprint-raggio-2549619.json"
    path.write_text(content)
    saved = read_fingerprint(path)
    assert saved is not None and saved != FP


FLAT = EngineState(chunks=EXPECTED)
IVF = EngineState(chunks=EXPECTED, ivf=True)
NONE = EngineState(chunks=None)
EMPTY = EngineState(chunks=0)
OTHER_FP = {**FP, "text_v": "real email embeddings"}


@pytest.mark.parametrize("name,state,saved,reingest,adopt,action", [
    ("raggio", NONE, None, False, False, "ingest"),
    ("raggio", EMPTY, None, False, False, "ingest"),
    ("raggio", FLAT, FP, False, False, "reuse"),
    ("raggio", IVF, FP, False, False, "reuse"),        # bench.py drops the index for the flat column
    ("raggio", FLAT, None, False, True, "reuse"),      # --adopt vouches for unfingerprinted data
    ("raggio", FLAT, FP, False, True, "reuse"),
    ("raggio", FLAT, FP, True, False, "ingest"),
    ("weaviate", FLAT, FP, False, False, "reuse"),
    ("weaviate", NONE, None, False, False, "ingest"),
    ("raggio-ivf", NONE, None, False, False, "ingest"),
    ("raggio-ivf", FLAT, FP, False, False, "build-index"),
    ("raggio-ivf", IVF, FP, False, False, "reuse"),
    ("raggio-ivf", IVF, FP, True, False, "build-index"),       # --reingest rebuilds the index only
    ("raggio-ivf", IVF, OTHER_FP, True, False, "ingest"),      # ... unless the data isn't this corpus
    ("raggio-ivf", EngineState(chunks=7), FP, True, False, "ingest"),
])
def test_decide(name, state, saved, reingest, adopt, action):
    assert decide(name, state, EXPECTED, saved, FP, reingest=reingest, adopt=adopt,
                  limit=2549619) == action


@pytest.mark.parametrize("name", ["raggio", "raggio-ivf", "weaviate"])
def test_wrong_limit_aborts_with_both_counts_and_the_flag(name):
    # --limit 2549119 (the ingested count) instead of 2549619 expects 500 fewer chunks
    with pytest.raises(BenchAbort) as e:
        decide(name, EngineState(chunks=2549119), 2549119 - 500, FP, FP,
               reingest=False, adopt=False, limit=2549119)
    msg = str(e.value)
    assert "2,549,119" in msg and "2,548,619" in msg and "--limit 2549119" in msg


@pytest.mark.parametrize("saved", [None, {}, OTHER_FP])
def test_fingerprint_mismatch_aborts_with_adopt_hint(saved):
    with pytest.raises(BenchAbort, match="--adopt"):
        decide("raggio", FLAT, EXPECTED, saved, FP, reingest=False, adopt=False, limit=2549619)


@pytest.mark.parametrize("saved", [{}, OTHER_FP])
def test_adopt_never_overrides_a_fingerprint_on_file(saved):
    # a different (or unreadable) fingerprint is evidence the volume holds other data
    with pytest.raises(BenchAbort, match="delete .* and pass --adopt"):
        decide("raggio", FLAT, EXPECTED, saved, FP, reingest=False, adopt=True, limit=2549619)


def test_adopt_never_overrides_a_count_mismatch():
    with pytest.raises(BenchAbort, match="--reingest"):
        decide("raggio", EngineState(chunks=10), EXPECTED, None, FP,
               reingest=False, adopt=True, limit=2549619)


class Calls:
    def __init__(self, state, ingested=EXPECTED):
        self.state_value, self.ingested, self.log = state, ingested, []

    async def state(self):
        self.log.append("state")
        if isinstance(self.state_value, Exception):
            raise self.state_value
        return self.state_value

    async def ingest(self):
        self.log.append("ingest")
        return 100.0, self.ingested

    async def build_index(self):
        self.log.append("build-index")
        return 42.0


def prepare(calls, name, fp_path, *, ivf=False, reingest=False, adopt=False):
    return asyncio.run(prepare_engine(
        name, state=calls.state, ingest=calls.ingest,
        build_index=calls.build_index if ivf else None, fp_path=fp_path, expected=EXPECTED,
        fp_now=FP, reingest=reingest, adopt=adopt, limit=2549619))


def test_prepare_reuse_touches_nothing(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, FP)
    calls = Calls(FLAT)
    assert prepare(calls, "raggio", fp_path) == {}
    assert calls.log == ["state"]


def test_prepare_ingest_times_it_and_records_the_fingerprint(tmp_path):
    fp_path = tmp_path / "fp.json"
    calls = Calls(NONE)
    res = prepare(calls, "raggio", fp_path)
    assert calls.log == ["state", "ingest"]
    assert res == {"ingest_s": 100.0, "ingest_vps": EXPECTED / 100.0}
    assert read_fingerprint(fp_path) == FP


def test_prepare_short_ingest_aborts_without_a_fingerprint(tmp_path):
    fp_path = tmp_path / "fp.json"
    with pytest.raises(BenchAbort, match="ingested 10"):
        prepare(Calls(NONE, ingested=10), "raggio", fp_path)
    assert read_fingerprint(fp_path) is None


def test_prepare_ivf_builds_the_index_on_reused_data(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, FP)
    calls = Calls(FLAT)
    assert prepare(calls, "raggio-ivf", fp_path, ivf=True) == {"index_build_s": 42.0}
    assert calls.log == ["state", "build-index"]


def test_prepare_ivf_on_an_empty_engine_ingests_then_builds(tmp_path):
    calls = Calls(NONE)
    assert prepare(calls, "raggio-ivf", tmp_path / "fp.json", ivf=True) == {"index_build_s": 42.0}
    assert calls.log == ["state", "ingest", "build-index"]


def test_prepare_adopt_records_the_fingerprint_without_ingesting(tmp_path):
    fp_path = tmp_path / "fp.json"
    calls = Calls(FLAT)
    assert prepare(calls, "raggio", fp_path, adopt=True) == {}
    assert calls.log == ["state"]
    assert read_fingerprint(fp_path) == FP


def test_prepare_adopt_keeps_a_different_fingerprint_and_touches_nothing(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, OTHER_FP)
    calls = Calls(FLAT)
    with pytest.raises(BenchAbort):
        prepare(calls, "raggio", fp_path, adopt=True)
    assert calls.log == ["state"]
    assert read_fingerprint(fp_path) == OTHER_FP


def test_prepare_unreachable_engine_never_ingests(tmp_path):
    calls = Calls(BenchAbort("unreachable"))
    with pytest.raises(BenchAbort):
        prepare(calls, "raggio", tmp_path / "fp.json")
    assert calls.log == ["state"]


def test_fingerprint_file_content_is_plain_json(tmp_path):
    fp_path = tmp_path / "fp.json"
    write_fingerprint(fp_path, FP)
    assert json.loads(fp_path.read_text()) == FP
