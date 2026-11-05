# ADR 0016 — Native search JSON validation through a shared route helper

Status: accepted after local evaluation; Sofia deployment validation pending · Date: 2026-11-05

Builds on [ADR 0008](0008-native-ingest-json.md), [ADR 0010](0010-search-response-encoding.md)
and the retrieval baseline in [ADR 0013](0013-scoped-search-batching.md).

## Context

Sofia's common document search uses hybrid retrieval with a supplied embedding
vector. Scoped batching improved concurrent summary searches, but left these
small document searches unchanged. The next evaluation targeted their request,
scoring and response work, with an explicit user adoption rule: a repeatable gain
of at least 10%, or 1–9% when the code also becomes simpler and clearer.

The local profile identified substantial CPU in decoding vector-bearing search
JSON. Reusing the native Pydantic parser already present for ingestion is smaller
than adding a prepared-document cache. The [evaluation report](../research/document-retrieval-evaluation-2026-11-05.md)
records the workloads, paired comparisons, controls and rejected cache prototype.

## Decision

Generalize the existing request/route helper to a route's single Pydantic body
model. `_ValidatedJSONRoute` captures the declared model from the body's field
metadata when the route is built, and `_ValidatedJSONRequest` validates JSON bytes
directly with that model's `model_validate_json` method.

Apply the shared route to search and to the existing ingest endpoint. Search uses
`SearchIn`; ingestion keeps using `IngestIn`. This adds no dependency, deployment
setting, persistent cache or public request field. It changes request processing,
not the retrieval algorithms or stored representation.

For ordinary valid JSON, the request caches the validated model and FastAPI can
reuse it. Reuse the original request's cached body. On native validation failure,
delegate JSON decoding to the original request and retain FastAPI's handling of
malformed bodies, alternate encodings, lone surrogates, validation details and
authentication/dependency precedence. These exceptional requests can still pay
the unsuccessful native attempt before the compatibility path.

The route is private and used only for endpoints declaring a single Pydantic body
model. It is not installed as an application-wide parser. The existing ingest
helper's private names change to describe their shared purpose; the ingestion
contract and its tests are retained.

## Evidence and limits

The controlled local seven-round comparison measures server CPU separately from
the HTTP client and alternates 32-request blocks with an unchanged-code control.
Its primary fixture uses full-precision JSON vectors of dimension 3072 and complete
chunk-text responses. Hybrid document p50 improves roughly 12–17% and CPU/query
roughly 10–17% at 1/8/16 clients, for both recurring and distinct documents.

Independent repeat results and complete raw measurements are in the report. Every
timed response must be byte-identical to the reference. This satisfies the local
adoption gate; it does not establish Sofia deployment gains. Smaller vector
payloads, different downstream retrieval costs and text-only requests can have
smaller effects. No meaningful text-only gain or controlled RSS reduction is claimed.
The local client shares the container CPU quota with the server, so QPS is not a
production capacity forecast.

## Rejected alternative

A benchmark-only normalized-vector cache helped selected hot vector workloads,
but did not show consistent hybrid benefit and worsened wider/cold access while
retaining memory. It would also need a complete write-invalidation and collection
lifecycle design. No production cache is added; the report and raw pilot preserve
that rejected evaluation.

## Validation

Focused native Linux checks pass 212 tests across the search parser, retained
ingest parser and API tests. They cover valid/invalid bodies, authentication order,
content types, UTF-16/UTF-32 and BOM fallback, surrogate handling, duplicate keys,
integer limits, cached models/bodies and unchanged OpenAPI. Ten thousand random
float-bit comparisons check numeric compatibility in addition to exact endpoint
response parity in the benchmark.

The final full suite, documentation build and pull-request CI results are recorded
in the evaluation report. Future comparisons should use the recorded source
hashes and repeat equivalent corpus/cache history; the historical Sofia baseline
remains labeled with the revision it actually measured.
