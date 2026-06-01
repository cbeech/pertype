"""Generate realistic, disjoint train/test corpora for json, logs, and html.

This stands in for real-world sample files so the benchmark can run end to end.
Each type produces many small files (the amortized "many files of a known type"
scenario). Train and test draw from independent random streams, so their
contents differ; the benchmark's hash check enforces disjointness regardless.
"""
import json
import os
import random

ROOT = os.path.join(os.path.dirname(__file__), "..", "corpus")

FIRST = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi",
         "ivan", "judy", "mallory", "olivia", "peggy", "trent", "victor", "wendy"]
LAST = ["smith", "jones", "khan", "garcia", "nguyen", "muller", "rossi", "kim",
        "santos", "ali", "brown", "wilson", "lee", "patel", "novak", "haddad"]
CITIES = ["london", "paris", "tokyo", "berlin", "madrid", "oslo", "lima", "cairo"]
STATUSES = ["active", "pending", "suspended", "closed"]
PATHS = ["/", "/index.html", "/api/users", "/api/orders/{}", "/login", "/logout",
         "/static/app.js", "/static/style.css", "/products/{}", "/cart"]
AGENTS = ["Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/124.0",
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0 Safari/537.36",
          "curl/8.5.0", "PostmanRuntime/7.36.0"]
LEVELS = ["INFO", "WARN", "ERROR", "DEBUG"]
SERVICES = ["auth", "payments", "orders", "inventory", "gateway", "scheduler"]
MESSAGES = ["request received", "user logged in", "cache miss", "db query slow",
            "payment authorized", "order created", "retrying connection",
            "token expired", "rate limit hit", "background job finished"]


def json_file(rng):
    obj = {
        "id": rng.randint(1000, 99999),
        "user": {
            "first_name": rng.choice(FIRST),
            "last_name": rng.choice(LAST),
            "email": f"{rng.choice(FIRST)}.{rng.choice(LAST)}@example.com",
            "city": rng.choice(CITIES),
        },
        "status": rng.choice(STATUSES),
        "balance": round(rng.uniform(0, 10000), 2),
        "active": rng.random() > 0.3,
        "roles": rng.sample(["admin", "user", "editor", "viewer", "billing"],
                            k=rng.randint(1, 3)),
        "items": [
            {"sku": f"SKU-{rng.randint(10000, 99999)}",
             "qty": rng.randint(1, 9),
             "price": round(rng.uniform(1, 500), 2)}
            for _ in range(rng.randint(1, 6))
        ],
    }
    return json.dumps(obj, indent=2).encode("utf-8")


def log_file(rng):
    lines = []
    for _ in range(rng.randint(20, 60)):
        ip = f"{rng.randint(1,255)}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,255)}"
        ts = f"2026-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}Z"
        path = rng.choice(PATHS).format(rng.randint(1, 9999))
        code = rng.choice([200, 200, 200, 301, 404, 500])
        size = rng.randint(120, 50000)
        agent = rng.choice(AGENTS)
        lines.append(f'{ip} - - [{ts}] "GET {path} HTTP/1.1" {code} {size} "{agent}"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def html_file(rng):
    rows = "\n".join(
        f'      <tr><td>{rng.choice(FIRST)} {rng.choice(LAST)}</td>'
        f'<td>{rng.choice(CITIES)}</td><td>{rng.choice(STATUSES)}</td>'
        f'<td>${round(rng.uniform(0, 9999), 2)}</td></tr>'
        for _ in range(rng.randint(5, 25))
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>Report {rng.randint(1, 9999)}</title>\n"
        "  <link rel=\"stylesheet\" href=\"/static/style.css\">\n"
        "</head>\n<body>\n  <header><nav><a href=\"/\">Home</a> "
        "<a href=\"/about\">About</a> <a href=\"/contact\">Contact</a></nav></header>\n"
        "  <main>\n    <h1>Customer Report</h1>\n    <table>\n"
        "      <thead><tr><th>Name</th><th>City</th><th>Status</th><th>Balance</th></tr></thead>\n"
        "      <tbody>\n" + rows + "\n      </tbody>\n    </table>\n  </main>\n"
        "  <footer><p>&copy; 2026 Example Corp. All rights reserved.</p></footer>\n"
        "</body>\n</html>\n"
    ).encode("utf-8")


GENERATORS = {"json": json_file, "logs": log_file, "html": html_file}


def write_split(type_id, gen, n_train=200, n_test=50):
    for split, count, seed in (("train", n_train, 1000), ("test", n_test, 9999)):
        d = os.path.join(ROOT, type_id, split)
        os.makedirs(d, exist_ok=True)
        rng = random.Random(seed + hash(type_id) % 1000)
        for i in range(count):
            with open(os.path.join(d, f"{i:04d}.{type_id}"), "wb") as fh:
                fh.write(gen(rng))
    print(f"{type_id}: wrote {n_train} train + {n_test} test files")


def main():
    for type_id, gen in GENERATORS.items():
        write_split(type_id, gen)


if __name__ == "__main__":
    main()
