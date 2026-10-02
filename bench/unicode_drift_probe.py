"""Unicode drift between two interpreters (ADR 0006): the 3.12 image has Unicode 15.0.0,
3.14t has 16.0.0, and the Python-side BM25 tokenizer (store._fold + store._TOKEN_RE) runs on
unicodedata at query time. This measures which code points tokenize differently between the
two, and how many corpus rows contain one, so a quality difference between the A/B arms
can be told apart from a threading one.

    # in each image (the dump needs raggio importable; diff and scan need only the stdlib)
    podman run --rm -i IMAGE python - dump < bench/unicode_drift_probe.py > u-IMAGE.json
    python bench/unicode_drift_probe.py diff u-a.json u-b.json > drift.json
    python bench/unicode_drift_probe.py scan drift.json --limit 2549619

Per code point only: context rules (Final_Sigma in str.lower, canonical reordering across
code points) are not modelled. SQLite's unicode61 tokenizer is compiled into the sqlite
library and does not move with the interpreter.
"""
import argparse
import json
import sys
import unicodedata

CORPUS = "bench/corpus/abstracts.jsonl"
FIRST_ROWS = 20  # how many drifted row numbers the scan lists
UNRECORDED = ["Cn", " "]  # what a dump leaves out: unassigned, and no word character


def dump(code_points=range(0x110000)) -> dict[str, list[str]]:
    """{str(cp): [general category, token signature]} for every code point that is
    assigned (not private use or a surrogate) or tokenizes to something. The signature is
    what the tokenizer makes of the code point alone: its _fold, with every character
    _TOKEN_RE would not keep as part of a token replaced by a space."""
    from raggio.store import _TOKEN_RE, _fold

    out = {}
    for cp in code_points:
        c = chr(cp)
        category = unicodedata.category(c)
        sig = "".join(ch if _TOKEN_RE.fullmatch(ch) else " " for ch in _fold(c))
        if category not in ("Cn", "Co", "Cs") or sig != " ":
            out[str(cp)] = [category, sig]
    return out


def diff(a: dict, b: dict) -> dict:
    """Code points new in b, code points recorded differently, and the ones whose token
    signature differs (the only ones that can move a BM25 score)."""
    ca, cb = a["cp"], b["cp"]
    keys = sorted({int(k) for k in (*ca, *cb)})
    newly, changed, drift = [], [], []
    for cp in keys:
        ra, rb = ca.get(str(cp), UNRECORDED), cb.get(str(cp), UNRECORDED)
        if ra[0] == "Cn" and rb[0] != "Cn":
            newly.append(cp)
        elif ra != rb:
            changed.append(cp)
        if ra[1] != rb[1]:
            drift.append(cp)
    return {"from": a["unidata"], "to": b["unidata"],
            "newly_assigned": newly, "changed": changed, "token_drift": drift}


def scan(drift: dict, corpus, limit: int | None = None) -> dict:
    """Rows of the bench corpus (title + text) that contain a code point of the diff."""
    any_drift = {chr(cp) for cp in (*drift["newly_assigned"], *drift["changed"], *drift["token_drift"])}
    token_drift = {chr(cp) for cp in drift["token_drift"]}
    rows = hits = token_hits = 0
    first = []
    with open(corpus, encoding="utf-8") as f:
        for line in f:
            if limit is not None and rows >= limit:
                break
            row = json.loads(line)
            chars = set(row.get("title") or "") | set(row.get("text") or "")
            if chars & any_drift:
                hits += 1
                if len(first) < FIRST_ROWS:
                    first.append(rows)
            if chars & token_drift:
                token_hits += 1
            rows += 1
    return {"rows": rows, "rows_with_drift": hits, "rows_with_token_drift": token_hits,
            "first_rows_with_drift": first}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("dump", help="this interpreter's per-code-point tokenization, as JSON")
    p = sub.add_parser("diff", help="compare two dumps")
    p.add_argument("a")
    p.add_argument("b")
    p = sub.add_parser("scan", help="count corpus rows touched by a diff")
    p.add_argument("drift")
    p.add_argument("--corpus", default=CORPUS)
    p.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    if args.mode == "dump":
        out = {"unidata": unicodedata.unidata_version, "python": sys.version, "cp": dump()}
    elif args.mode == "diff":
        with open(args.a, encoding="utf-8") as fa, open(args.b, encoding="utf-8") as fb:
            out = diff(json.load(fa), json.load(fb))
        print(f"{out['from']} -> {out['to']}: {len(out['newly_assigned'])} newly assigned, "
              f"{len(out['changed'])} changed, {len(out['token_drift'])} with token drift",
              file=sys.stderr)
    else:
        with open(args.drift, encoding="utf-8") as f:
            out = scan(json.load(f), args.corpus, args.limit)
    json.dump(out, sys.stdout, ensure_ascii=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
