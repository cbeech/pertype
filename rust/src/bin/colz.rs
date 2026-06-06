//! `colz` — a standalone Rust CLI for the columnar record codec, no Python needed.
//! Output is the same `COL1` container the Python codec produces, so files are
//! interchangeable between the two.
//!
//!   colz enc <in> <out> [width]   # width omitted / 0 = auto-detect the record period
//!   colz dec <in> <out>

use std::fs;
use std::process::exit;

use compressor_rs::columnar;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 {
        eprintln!("usage: colz enc <in> <out> [width] | colz dec <in> <out>");
        exit(2);
    }
    let data = fs::read(&args[2]).unwrap_or_else(|e| {
        eprintln!("read {}: {e}", args[2]);
        exit(1);
    });
    let out = match args[1].as_str() {
        "enc" => {
            let width: usize = args.get(4).and_then(|w| w.parse().ok()).unwrap_or(0);
            let blob = columnar::encode(&data, width);
            let ratio = data.len() as f64 / blob.len() as f64;
            eprintln!("{}: {} -> {} bytes ({ratio:.2}x)", args[2], data.len(), blob.len());
            blob
        }
        "dec" => columnar::decode(&data),
        other => {
            eprintln!("unknown command {other:?}");
            exit(2);
        }
    };
    fs::write(&args[3], out).unwrap_or_else(|e| {
        eprintln!("write {}: {e}", args[3]);
        exit(1);
    });
}
