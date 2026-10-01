//! raggio_native: raggio.store's stage-2 BM25 scorer and tokenizer, run without the GIL.
//!
//! `fold_tokens(text)` equals `raggio.store._fold_tokens(text)` for every str without
//! lone surrogates: str.lower() (with CPython's Final_Sigma rule), NFKD, drop combining
//! marks, split on `[^\W_]+`. The Unicode data comes from the interpreter the module
//! was built for (build.rs -> gen_unicode.py), exposed as UNIDATA_VERSION.
//! `bm25_topn(...)` equals `raggio.store._bm25_topn(...)`: the same ids in the same
//! order and bit-identical scores (ADR 0004).
use std::cmp::Ordering;
use std::collections::HashMap;

use pyo3::exceptions::{PyValueError, PyZeroDivisionError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedStr;
use pyo3::types::PyList;
use rustc_hash::FxHashMap;

mod tables {
    include!(concat!(env!("OUT_DIR"), "/unicode_tables.rs"));
}

const CAPITAL_SIGMA: char = '\u{3A3}';

#[inline]
fn in_ranges(ranges: &[(u32, u32)], cp: u32) -> bool {
    ranges
        .binary_search_by(|&(lo, hi)| {
            if hi < cp {
                Ordering::Less
            } else if lo > cp {
                Ordering::Greater
            } else {
                Ordering::Equal
            }
        })
        .is_ok()
}

/// A char of the `[^\W_]` class (str.isalnum) in the build interpreter's Unicode data.
#[inline]
fn is_word(c: char) -> bool {
    if c.is_ascii() {
        c.is_ascii_alphanumeric()
    } else {
        in_ranges(tables::WORD, c as u32)
    }
}

/// `_fold(c)` for one char when it differs from `c` (Σ excluded: see final_sigma).
#[inline]
fn fold_of(c: char) -> Option<&'static [char]> {
    let i = tables::FOLD_KEYS.binary_search(&(c as u32)).ok()?;
    Some(&tables::FOLD_CHARS[tables::FOLD_OFFS[i] as usize..tables::FOLD_OFFS[i + 1] as usize])
}

/// CPython's handle_capital_sigma: the Σ at byte offset `i` lowers to final ς iff
/// \p{Cased} \p{Case_Ignorable}* Σ !(\p{Case_Ignorable}* \p{Cased}), read on the
/// original (not yet lowered) text. CASED holds only the cased chars that are not
/// case-ignorable, the only ones either scan can stop on.
fn final_sigma(text: &str, i: usize) -> bool {
    let is_ci = |c: char| in_ranges(tables::CASE_IGNORABLE, c as u32);
    let is_cased = |c: char| in_ranges(tables::CASED, c as u32);
    match text[..i].chars().rev().find(|&c| !is_ci(c)) {
        Some(c) if is_cased(c) => {}
        _ => return false,
    }
    let after = &text[i + CAPITAL_SIGMA.len_utf8()..];
    !matches!(after.chars().find(|&c| !is_ci(c)), Some(c) if is_cased(c))
}

/// Emit `"".join(c for c in NFKD(text.lower()) if not combining(c))` char by char.
/// Per-char folding is exact: str.lower() maps chars independently except Σ, and NFKD's
/// canonical reordering only moves combining marks, which are all dropped.
#[inline]
fn fold_chars(text: &str, mut emit: impl FnMut(char)) {
    if text.is_ascii() {
        for b in text.bytes() {
            emit(b.to_ascii_lowercase() as char);
        }
        return;
    }
    let mut one = [' '];
    for (i, c) in text.char_indices() {
        let folded: &[char] = if c.is_ascii() {
            one[0] = c.to_ascii_lowercase();
            &one
        } else if c == CAPITAL_SIGMA {
            one[0] = if final_sigma(text, i) { '\u{3C2}' } else { '\u{3C3}' };
            &one
        } else if let Some(folded) = fold_of(c) {
            folded
        } else {
            one[0] = c;
            &one
        };
        folded.iter().for_each(|&d| emit(d)); // one call site: emit inlines once
    }
}

/// Call `on_token` for each `[^\W_]+` run of the folded text; `buf` is scratch space.
#[inline]
fn for_each_token(text: &str, buf: &mut String, mut on_token: impl FnMut(&str)) {
    buf.clear();
    fold_chars(text, |c| {
        if is_word(c) {
            buf.push(c);
        } else if !buf.is_empty() {
            on_token(buf);
            buf.clear();
        }
    });
    if !buf.is_empty() {
        on_token(buf);
    }
}

/// raggio.store._fold_tokens(text), computed with the GIL released.
#[pyfunction]
fn fold_tokens(py: Python<'_>, text: PyBackedStr) -> Vec<String> {
    py.detach(move || {
        let mut out = Vec::new();
        let mut buf = String::new();
        for_each_token(&text, &mut buf, |t| out.push(t.to_owned()));
        out
    })
}

/// CPython's builtin sum() over floats (Neumaier-compensated since 3.12), so each score
/// is bit-identical to the reference's, not merely close.
#[derive(Default)]
struct PySum {
    s: f64,
    c: f64,
}

impl PySum {
    #[inline]
    fn add(&mut self, x: f64) {
        let t = self.s + x;
        if self.s.abs() >= x.abs() {
            self.c += (self.s - t) + x;
        } else {
            self.c += (x - t) + self.s;
        }
        self.s = t;
    }

    #[inline]
    fn get(&self) -> f64 {
        if self.c != 0.0 && self.c.is_finite() {
            self.s + self.c
        } else {
            self.s
        }
    }
}

const NOT_A_TERM: u32 = u32::MAX;

/// One scored candidate. `int_zero` marks the reference's `sum(())` result: no query
/// token in the text and no query bigrams, where Python's score is the int 0.
struct Scored {
    score: f64,
    rid: i64,
    int_zero: bool,
}

/// raggio.store._bm25_topn: top-n (ids, scores) of BM25 + SDM-lite over the candidates
/// (rids[i], texts[i]), ordered by (-score, rid). The GIL is released while scoring.
#[pyfunction]
#[pyo3(signature = (qtoks, idf, rids, texts, avgdl, n, k1, b, sdm_weight))]
#[allow(clippy::too_many_arguments)]
fn bm25_topn<'py>(
    py: Python<'py>,
    qtoks: Vec<String>,
    idf: HashMap<String, f64>,
    rids: Vec<i64>,
    texts: Vec<Option<PyBackedStr>>,
    avgdl: f64,
    n: usize,
    k1: f64,
    b: f64,
    sdm_weight: f64,
) -> PyResult<(Vec<i64>, Bound<'py, PyList>)> {
    if rids.len() != texts.len() {
        return Err(PyValueError::new_err("rids and texts differ in length"));
    }
    if qtoks.is_empty() || texts.is_empty() {
        return Ok((vec![], PyList::empty(py)));
    }
    if avgdl == 0.0 {
        return Err(PyZeroDivisionError::new_err("float division by zero"));
    }
    // query terms indexed in first-occurrence order (the reference's dict.fromkeys)
    let mut term_ix: FxHashMap<&str, u32> = FxHashMap::default();
    let mut term_idf: Vec<f64> = Vec::new();
    for t in &qtoks {
        if !term_ix.contains_key(t.as_str()) {
            let v = *idf
                .get(t)
                .ok_or_else(|| PyValueError::new_err(format!("no idf for query token {t:?}")))?;
            term_ix.insert(t.as_str(), term_idf.len() as u32);
            term_idf.push(v);
        }
    }
    if idf.len() != term_idf.len() {
        return Err(PyValueError::new_err("idf has keys that are not query tokens"));
    }
    let top = py.detach(|| {
        let nt = term_idf.len();
        // ordered query bigrams (x != y) as a dense nt x nt membership table
        let mut is_pair = vec![false; nt * nt];
        let mut any_pair = false;
        for w in qtoks.windows(2) {
            let (x, y) = (term_ix[w[0].as_str()], term_ix[w[1].as_str()]);
            if x != y {
                is_pair[x as usize * nt + y as usize] = true;
                any_pair = true;
            }
        }
        let k1p1 = k1 + 1.0;
        let mut tf = vec![0u32; nt];
        let mut order: Vec<u32> = Vec::with_capacity(nt); // the reference's dict order
        let mut tf2: FxHashMap<(u32, u32), u32> = FxHashMap::default();
        let mut order2: Vec<(u32, u32)> = Vec::new();
        let mut buf = String::new();
        let mut scored: Vec<Scored> = Vec::with_capacity(texts.len());
        for (rid, text) in rids.iter().zip(texts.iter()) {
            tf.iter_mut().for_each(|x| *x = 0);
            order.clear();
            tf2.clear();
            order2.clear();
            let mut dl: u64 = 0;
            let mut prev = NOT_A_TERM;
            for_each_token(text.as_deref().unwrap_or(""), &mut buf, |tok| {
                dl += 1;
                let cur = term_ix.get(tok).copied().unwrap_or(NOT_A_TERM);
                if cur != NOT_A_TERM {
                    if tf[cur as usize] == 0 {
                        order.push(cur);
                    }
                    tf[cur as usize] += 1;
                    if any_pair && prev != NOT_A_TERM && is_pair[prev as usize * nt + cur as usize] {
                        let e = tf2.entry((prev, cur)).or_insert(0);
                        if *e == 0 {
                            order2.push((prev, cur));
                        }
                        *e += 1;
                    }
                }
                prev = cur;
            });
            // the reference's expressions, operation for operation: IEEE-754 results then
            // match bit for bit (Rust never fuses a multiply-add on its own)
            let dl = if dl == 0 { 1.0 } else { dl as f64 };
            let norm = k1 * (1.0 - b + b * dl / avgdl);
            let mut s = PySum::default();
            for &t in &order {
                let f = tf[t as usize] as f64;
                s.add(term_idf[t as usize] * f * k1p1 / (f + norm));
            }
            let mut score = s.get();
            if any_pair {
                let mut s2 = PySum::default();
                for pr in &order2 {
                    let f = tf2[pr] as f64;
                    let pair_idf = term_idf[pr.0 as usize] + term_idf[pr.1 as usize];
                    s2.add(pair_idf / 2.0 * f * k1p1 / (f + norm));
                }
                score += sdm_weight * s2.get();
            }
            scored.push(Scored { score, rid: *rid, int_zero: order.is_empty() && !any_pair });
        }
        // (-score, rid) ascending; scores are never NaN (idf >= 1e-6, norm > 0)
        let cmp = |x: &Scored, y: &Scored| {
            y.score.partial_cmp(&x.score).unwrap_or(Ordering::Equal).then(x.rid.cmp(&y.rid))
        };
        if n < scored.len() {
            scored.select_nth_unstable_by(n, cmp);
            scored.truncate(n);
        }
        scored.sort_unstable_by(cmp);
        scored
    });
    let ids = top.iter().map(|x| x.rid).collect();
    let scores = PyList::empty(py);
    for x in &top {
        if x.int_zero {
            scores.append(0)?;
        } else {
            scores.append(x.score)?;
        }
    }
    Ok((ids, scores))
}

#[pymodule(gil_used = false)]
fn raggio_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("UNIDATA_VERSION", tables::UNIDATA_VERSION)?;
    m.add_function(wrap_pyfunction!(fold_tokens, m)?)?;
    m.add_function(wrap_pyfunction!(bm25_topn, m)?)?;
    Ok(())
}
