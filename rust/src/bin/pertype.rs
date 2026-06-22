//! `pertype` — the standalone Rust CLI (no Python). Mirrors the Python `pertype`
//! command's core flow: train a per-type model, then `compress` / `decompress` with a
//! self-describing container that routes itself.
//!
//!   pertype train <type_id> <corpus_dir> -o <model>
//!   pertype compress   <in> [-m <model>] [-o <out>]   # auto-routes; --model also tries the
//!                                                          # trained codec, smaller wins (.cmp)
//!   pertype decompress <in> [-m <model>] [-o <out>]   # sniffs the container
//!
//! Output is cross-compatible with the Python tool (byte-identical for the arithmetic-coded
//! parts; the auto-router's deflate sub-blobs differ but decode in both).

use std::fs;
use std::process::exit;

use pertype::{auto, textcodec};

const CZ_MAGIC: u8 = 0xC7; // trained-model container
const AZ_MAGIC: &[u8] = b"AZ"; // auto container

fn die(msg: &str) -> ! {
    eprintln!("{msg}");
    exit(2);
}

/// Pull `-m/--model` and `-o/--output` out of the args; return (positionals, model, output).
fn parse_opts(args: &[String]) -> (Vec<String>, Option<String>, Option<String>) {
    let (mut pos, mut model, mut output) = (Vec::new(), None, None);
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "-m" | "--model" => {
                i += 1;
                model = Some(args.get(i).cloned().unwrap_or_else(|| die("missing value for --model")));
            }
            "-o" | "--output" => {
                i += 1;
                output = Some(args.get(i).cloned().unwrap_or_else(|| die("missing value for --output")));
            }
            a => pos.push(a.to_string()),
        }
        i += 1;
    }
    (pos, model, output)
}

fn read(path: &str) -> Vec<u8> {
    fs::read(path).unwrap_or_else(|e| die(&format!("read {path}: {e}")))
}
fn write(path: &str, data: &[u8]) {
    fs::write(path, data).unwrap_or_else(|e| die(&format!("write {path}: {e}")));
}

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    if argv.len() < 2 {
        die("usage: pertype <train|compress|decompress> <in> [-m <model>] [-o <out>]");
    }
    let rest = &argv[2..];
    match argv[1].as_str() {
        "train" => {
            let (pos, _m, output) = parse_opts(rest);
            if pos.len() != 2 {
                die("usage: pertype train <type_id> <corpus_dir> -o <model>");
            }
            let out = output.unwrap_or_else(|| die("train needs -o <model>"));
            let mut entries: Vec<_> = fs::read_dir(&pos[1])
                .unwrap_or_else(|e| die(&format!("read dir {}: {e}", pos[1])))
                .filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| p.is_file())
                .collect();
            entries.sort();
            let samples: Vec<Vec<u8>> = entries.iter().map(|p| fs::read(p).unwrap()).collect();
            if samples.is_empty() {
                die(&format!("no files in {}", pos[1]));
            }
            let refs: Vec<&[u8]> = samples.iter().map(|v| v.as_slice()).collect();
            let model = textcodec::train(&refs, &pos[0], 4096, 3, 256);
            write(&out, &model);
            eprintln!("trained '{}' on {} files -> {} ({} bytes)", pos[0], samples.len(), out, model.len());
        }
        "compress" => {
            let (pos, model, output) = parse_opts(rest);
            if pos.len() != 1 {
                die("usage: pertype compress <in> [-m <model>] [-o <out>]");
            }
            let data = read(&pos[0]);
            let mut best = auto::encode(&data);
            let mut tag = format!("auto/{}", auto::method_name(&best));
            if let Some(mp) = model {
                let cz = textcodec::compress(&read(&mp), &data);
                if cz.len() < best.len() {
                    best = cz;
                    tag = "trained-model".to_string();
                }
            }
            let dest = output.unwrap_or_else(|| format!("{}.cmp", pos[0]));
            let ratio = data.len() as f64 / best.len().max(1) as f64;
            write(&dest, &best);
            eprintln!("{}: {} -> {} bytes ({ratio:.2}x) [{tag}] -> {dest}", pos[0], data.len(), best.len());
        }
        "decompress" => {
            let (pos, model, output) = parse_opts(rest);
            if pos.len() != 1 {
                die("usage: pertype decompress <in> [-m <model>] [-o <out>]");
            }
            let data = read(&pos[0]);
            let out = if data.first() == Some(&CZ_MAGIC) {
                let mp = model.unwrap_or_else(|| die("this file needs -m <model> (trained-model container)"));
                textcodec::decompress(&read(&mp), &data)
            } else if data.starts_with(AZ_MAGIC) {
                auto::decode(&data)
            } else {
                die("unrecognized container (not a pertype .cmp/.cz/.az file)");
            };
            let dest = output.unwrap_or_else(|| {
                pos[0].strip_suffix(".cmp").map(str::to_string).unwrap_or_else(|| format!("{}.out", pos[0]))
            });
            write(&dest, &out);
            eprintln!("{}: -> {} bytes -> {dest}", pos[0], out.len());
        }
        other => die(&format!("unknown command '{other}' (train|compress|decompress)")),
    }
}
