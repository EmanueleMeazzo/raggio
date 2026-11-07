"""bench/prep_wiki.py and bench/bench_docs.py: how an article becomes a document (summary +
chunks), the query sets, the request each engine gets (both spellings of the one-document filter),
quality scoring and the exact top-k behind the chunks-k* recall."""
import sys

import numpy as np
import pytest

pytest.importorskip("orjson", reason="the bench harness needs the bench dependency group")
sys.path.insert(0, "bench")
import bench_docs as bd  # noqa: E402
import prep_wiki as pw  # noqa: E402


def article(lead_words=120, sections=14, para_words=220):
    lead = " ".join(["Alpha is a test article about things."] * (lead_words // 7))
    body = []
    for s in range(sections):
        body += [f"Section {s}", " ".join(["This test sentence has exactly nine words in it."] * (para_words // 9)), ""]
    return lead + "\n\n" + "\n".join(body)


def test_headings_are_short_lines_without_closing_punctuation():
    assert pw.is_heading("Operational history")
    assert pw.is_heading("See also")
    assert not pw.is_heading("HMAS Sydney, named after the Australian city of Sydney.")
    assert not pw.is_heading("")
    assert not pw.is_heading("one two three four five six seven eight nine ten eleven")


def test_split_keeps_the_lead_apart_and_headings_with_their_paragraph():
    lead, body = pw.split_article("Lead one.\n\nLead two.\n\nHistory\nFirst para.\n\nDesign\n\nSecond para.")
    assert lead == ["Lead one.", "Lead two."]
    assert body == ["History\nFirst para.", "Design\nSecond para."]


def test_chunks_close_at_the_target_and_fold_a_short_tail():
    blocks = ["w " * 500, "w " * 400, "w " * 900, "w " * 100]
    chunks = pw.make_chunks(blocks, target=800, min_tail=200, max_chunks=10)
    assert [pw.words(c) for c in chunks] == [900, 1000]  # 500+400 closes; 900 closes; 100 folds in
    assert len(pw.make_chunks(["w " * 800] * 30, target=800, max_chunks=10)) == 10


def test_an_article_becomes_one_summary_and_its_chunks():
    recs = pw.shape_article("7", "Alpha", article())
    assert recs[0] == {"id": "w7", "doc": "w7", "kind": "summary", "title": "Alpha",
                       "text": pw.make_summary(pw.split_article(article())[0])}
    assert pw.words(recs[0]["text"]) <= pw.SUMMARY_WORDS
    assert [r["id"] for r in recs[1:]] == [f"w7#{i}" for i in range(len(recs) - 1)]
    assert {r["kind"] for r in recs[1:]} == {"chunk"} and all(r["doc"] == "w7" for r in recs)


@pytest.mark.parametrize("text,title", [(article(lead_words=20), "Alpha"),     # lead too short
                                         (article(sections=4), "Alpha"),       # body too short
                                         (article(), "List of alphas")])       # list page
def test_articles_that_are_not_document_shaped_are_skipped(text, title):
    assert pw.shape_article("1", title, text) is None


def test_query_sets_are_deterministic_and_aim_at_their_targets():
    recs = [r for i in range(40) for r in pw.shape_article(str(i), f"T{i}", article())]
    q1, q2 = pw.build_queries(recs, n_summary=6, n_indoc=6), pw.build_queries(recs, n_summary=6, n_indoc=6)
    assert q1 == q2
    by_id = {r["id"]: r for r in recs}
    summ = [q for q in q1 if q["set"] == "summary"]
    indoc = [q for q in q1 if q["set"] == "indoc"]
    assert len(summ) == 6 and len(indoc) == 6
    assert all(by_id[q["target"]]["kind"] == "summary" and q["text"] == by_id[q["target"]]["title"] for q in summ)
    assert all(by_id[q["target"]]["kind"] == "chunk" and q["text"] in by_id[q["target"]]["text"]
               and q["doc"] == by_id[q["target"]]["doc"] for q in indoc)


def test_queries_carry_the_embedding_instruction():
    assert pw.query_input("HMAS Sydney") == f"Instruct: {pw.QUERY_INSTRUCTION}\nQuery:HMAS Sydney"


Q = {"vector": np.array([0.5, 0.25], np.float32), "text": "a \"quoted\" title", "doc": "w7", "target": "w7"}


def test_raggio_document_filter_has_two_spellings():
    _, body = bd.raggio_request(Q, "chunks", 5, False, True, "doc_id")
    assert body["filter"] == {"doc_id": "w7"} and body["scope"] == "chunks" and body["k"] == 5
    _, body = bd.raggio_request(Q, "chunks", 5, True, True, "metadata")
    assert body["filter"] == {"doc": "w7"} and body["mode"] == "hybrid" and body["query"]["text"] == Q["text"]
    _, body = bd.raggio_request(Q, "summaries", 10, False, False, "doc_id")
    assert "filter" not in body and "mode" not in body and body["scope"] == "summaries"
    with pytest.raises(ValueError):
        bd.doc_filter("w7", "other")


def test_weaviate_request_scopes_by_kind_and_document_and_returns_the_same_fields():
    _, body = bd.weaviate_request(Q, "summaries", 10, False, False)
    gql = body["query"]
    assert 'where: {path: ["kind"], operator: Equal, valueText: "summary"}' in gql
    assert "nearVector" in gql and "limit: 10" in gql and bd.WV_FIELDS in gql
    _, body = bd.weaviate_request(Q, "chunks", 5, True, True)
    gql = body["query"]
    assert "operator: And" in gql and 'valueText: "chunk"' in gql and 'valueText: "w7"' in gql
    assert 'hybrid: {query: "a \\"quoted\\" title"' in gql and "fusionType: rankedFusion" in gql


def test_hits_parse_per_engine_and_graphql_errors_raise():
    assert bd.parse_hits("raggio-ivf", {"hits": [{"id": "w7"}, {"id": "w7#0"}]}) == ["w7", "w7#0"]
    resp = {"data": {"Get": {bd.WCLASS: [{"recId": "w7#1"}]}}}
    assert bd.parse_hits("weaviate", resp) == ["w7#1"]
    with pytest.raises(RuntimeError):
        bd.parse_hits("weaviate", {"errors": [{"message": "boom"}]})


def test_quality_is_target_hit_rate_or_recall_at_k():
    qs = [{"target": "a"}, {"target": "b"}]
    assert bd.quality("summary-vector", qs, [["a", "x"], ["x"]]) == 0.5
    assert bd.quality("indoc-hybrid", qs, [["a"], ["b"]]) == 1.0
    assert bd.quality("chunks-k10", qs, [["r1", "r2"], ["r3"]], truth=[["r1", "r9"], ["r3", "r4"]], k=2) == 0.5


def test_exact_topk_ranks_only_chunks_by_cosine(tmp_path):
    records = [{"kind": "summary"}, {"kind": "chunk"}, {"kind": "chunk"}, {"kind": "chunk"}]
    vecs = np.array([[1, 0], [0.6, 0.8], [0.8, 0.6], [0, 1]], np.float32)
    queries = [{"vector": np.array([1, 0], np.float32)}, {"vector": np.array([0, 1], np.float32)}]
    out = bd.exact_topk(records, vecs, queries, ks=(1, 2), cache=tmp_path / "gt.npz")
    assert out[1].tolist() == [[2], [3]]        # the summary row (0) never ranks
    assert out[2].tolist() == [[2, 1], [3, 1]]
    again = bd.exact_topk(records, vecs, queries, ks=(1, 2), cache=tmp_path / "gt.npz")
    assert again[2].tolist() == out[2].tolist()


def test_ingest_payload_groups_a_document_summary_first():
    records = [{"id": "w1", "doc": "w1", "kind": "summary", "title": "T", "text": "s"},
               {"id": "w1#0", "doc": "w1", "kind": "chunk", "title": "T", "text": "c0"},
               {"id": "w2", "doc": "w2", "kind": "summary", "title": "U", "text": "s2"}]
    vecs = np.eye(3, dtype=np.float32)
    groups = bd.by_document(records)
    assert [[r["id"] for r in g] for g, _ in groups] == [["w1", "w1#0"], ["w2"]]
    doc = bd.raggio_doc(groups[0][0], vecs, groups[0][1])
    assert doc["doc_id"] == "w1" and doc["summary"]["metadata"] == {"doc": "w1", "title": "T", "kind": "summary"}
    assert doc["chunks"][0]["id"] == "w1#0" and doc["chunks"][0]["position"] == 0
    assert doc["chunks"][0]["metadata"]["doc"] == "w1"
