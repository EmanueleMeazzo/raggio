//! Generates $OUT_DIR/unicode_tables.rs by running gen_unicode.py with the interpreter
//! this extension is built for, so the tokenizer's Unicode data is that interpreter's
//! unicodedata (see gen_unicode.py). maturin sets PYO3_PYTHON; a bare `cargo build`
//! falls back to python3 (python on Windows) from PATH.
use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=gen_unicode.py");
    println!("cargo:rerun-if-env-changed=PYO3_PYTHON");
    let python = env::var("PYO3_PYTHON")
        .unwrap_or_else(|_| if cfg!(windows) { "python" } else { "python3" }.to_string());
    let script = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join("gen_unicode.py");
    let out = PathBuf::from(env::var("OUT_DIR").unwrap()).join("unicode_tables.rs");
    let status = Command::new(&python)
        .arg(&script)
        .arg(&out)
        .status()
        .unwrap_or_else(|e| panic!("cannot run {python} {}: {e}", script.display()));
    assert!(status.success(), "{python} {} failed: {status}", script.display());
}
