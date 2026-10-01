//! raggio_native: the stage-2 BM25 tokenizer of raggio.store, without the GIL.
//!
//! `fold_tokens(text)` equals `raggio.store._fold_tokens(text)` for every str without
//! lone surrogates: str.lower() (with CPython's Final_Sigma rule), NFKD, drop combining
//! marks, split on `[^\W_]+`. The Unicode data comes from the interpreter the module
//! was built for (build.rs -> gen_unicode.py), exposed as UNIDATA_VERSION.
use std::cmp::Ordering;

use pyo3::prelude::*;
use pyo3::pybacked::PyBackedStr;

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

#[pymodule(gil_used = false)]
fn raggio_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("UNIDATA_VERSION", tables::UNIDATA_VERSION)?;
    m.add_function(wrap_pyfunction!(fold_tokens, m)?)?;
    Ok(())
}
