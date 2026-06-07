//! `azc` — a standalone Rust auto-compressor. Detects/routes among the Rust codecs
//! (deflate / CSV-columnar / record-columnar / store), verifies, and writes the same `AZ`
//! container the Python `auto` produces — so the output is decodable by both.
//!
//!   azc enc <in> <out>
//!   azc dec <in> <out>

use std::fs;
use std::process::exit;

use compressor_rs::auto;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 || (args[1] != "enc" && args[1] != "dec") {
        eprintln!("usage: azc enc <in> <out> | azc dec <in> <out>");
        exit(2);
    }
    let data = fs::read(&args[2]).unwrap_or_else(|e| {
        eprintln!("read {}: {e}", args[2]);
        exit(1);
    });
    let out = if args[1] == "enc" {
        let blob = auto::encode(&data);
        let ratio = data.len() as f64 / blob.len() as f64;
        eprintln!("{}: {} -> {} bytes ({ratio:.2}x) [{}]", args[2], data.len(), blob.len(),
                  auto::method_name(&blob));
        blob
    } else {
        auto::decode(&data)
    };
    fs::write(&args[3], out).unwrap_or_else(|e| {
        eprintln!("write {}: {e}", args[3]);
        exit(1);
    });
}
