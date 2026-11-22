"""End-to-end HTTP test of the exact demo flow through a real gunicorn-ish app.

Run manually with ``python3 scripts/demo_http.py`` (uses Flask's test client,
so no network needed) or point ``FLOOD_DEMO_URL`` at a running server.

The flow: import the base samples, exercise retry/conflict, then re-survey
with v8/v9 so the older coverage rectangles become redundant, compact the
coverage index, verify box queries and cell verdicts are unchanged, and
finally land a late old package to show highest-version judging still works.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "app" / "static" / "samples"


def flow(request) -> int:
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    def import_sample(name):
        return request("/api/packages", (SAMPLES / name).read_bytes())

    for name in ("v3.json", "v7.json", "v5-late.json"):
        code, body = import_sample(name)
        check(name, code == 201 and body.get("status") == "imported", f"HTTP {code} {body.get('status')}")

    code, body = import_sample("v7-retry.json")
    check("v7-retry.json", code == 200 and body.get("status") == "unchanged", f"HTTP {code} {body.get('status')}")
    code, body = import_sample("v7-conflict.json")
    check("v7-conflict.json", code == 409 and body.get("error") == "version_conflict", f"HTTP {code} {body.get('error')}")

    _, wet_cell = request("/api/cell?row=8&col=5")
    _, dry_cell = request("/api/cell?row=22&col=7")
    _, unknown_cell = request("/api/cell?row=500&col=500")
    check("cell(8,5) wet", wet_cell["status"] == "wet" and wet_cell["version"] == 7, str(wet_cell))
    check("cell(22,7) dry", dry_cell["status"] == "dry", str(dry_cell))
    check("cell(500,500) unsurveyed", unknown_cell["status"] == "unsurveyed", str(unknown_cell))

    # Re-survey in two halves.  v8+v9 together cover v3, v5 and v7: v3 and
    # v5 are each swallowed by one newer rect, v7 only by the two tiled.
    for name in ("v8-south.json", "v9-north.json"):
        code, body = import_sample(name)
        check(name, code == 201, f"HTTP {code} {body.get('status')}")

    box = "/api/wet?row_start=0&row_end=30&col_start=0&col_end=30&limit=5000"
    _, before = request(box)
    _, listed = request("/api/packages")
    check("5 rects before compact", listed["coverage_rect_count"] == 5, str(listed["coverage_rect_count"]))

    code, compacted = request("/api/compact", b"")
    check(
        "compact removes v3,v5,v7",
        code == 200
        and compacted.get("status") == "compacted"
        and compacted.get("removed_versions") == [3, 5, 7]
        and compacted.get("rects_after") == 2,
        f"HTTP {code} {compacted}",
    )

    _, after = request(box)
    check(
        "box query identical, fewer rects",
        after["cells"] == before["cells"]
        and len(after["coverage_rects"]) == 2
        and len(before["coverage_rects"]) == 5,
        f"rects {len(before['coverage_rects'])} -> {len(after['coverage_rects'])}, cells {len(after['cells'])}",
    )
    _, north = request("/api/cell?row=8&col=5")
    _, south = request("/api/cell?row=22&col=2")
    _, dried = request("/api/cell?row=3&col=15")
    check("cell(8,5) wet v9", north["status"] == "wet" and north["version"] == 9, str(north))
    check("cell(22,2) wet v8", south["status"] == "wet" and south["version"] == 8, str(south))
    check("cell(3,15) dry v9", dried["status"] == "dry" and dried["version"] == 9, str(dried))

    # A late old package is still judged by the highest version: both of its
    # listed ponds are shadowed by v8/v9 even though v3/v5/v7 are gone.
    code, late = import_sample("v4-late.json")
    summary = late.get("summary", {})
    check(
        "v4 late fully shadowed",
        code == 201
        and summary.get("current_wet_added") == 0
        and summary.get("wet_points_shadowed_by_newer") == 2,
        f"HTTP {code} {summary}",
    )

    code, again = request("/api/compact", b"")
    check(
        "second compact removes v4",
        code == 200 and again.get("removed_versions") == [4] and again.get("rects_after") == 2,
        f"HTTP {code} {again}",
    )
    _, final = request(box)
    check("cells still identical", final["cells"] == before["cells"], f"{len(final['cells'])} cells")
    code, noop = request("/api/compact", b"")
    check("third compact is noop", code == 200 and noop.get("status") == "noop", f"HTTP {code} {noop.get('status')}")
    return report(checks)


def via_url(base: str) -> int:
    def request(path, data=None):
        req = urllib.request.Request(base + path, data=data)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    return flow(request)


def via_test_client() -> int:
    import tempfile

    from app import create_app

    tmp = tempfile.mkdtemp(prefix="flood-demo-")
    app = create_app({"TESTING": True, "DATABASE": f"{tmp}/demo.sqlite3", "ALLOW_RESET": True})
    client = app.test_client()

    def request(path, data=None):
        if data is None:
            resp = client.get(path)
        else:
            resp = client.post(path, data=data, content_type="application/json")
        return resp.status_code, resp.get_json()

    return flow(request)


def report(checks) -> int:
    failed = 0
    for name, ok, detail in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name:38} {detail}")
        if not ok:
            failed += 1
    if failed:
        print(f"\n{failed} unexpected result(s).")
    else:
        print("\nAll demo steps behaved as expected.")
    return 1 if failed else 0


if __name__ == "__main__":
    url = os.environ.get("FLOOD_DEMO_URL")
    sys.exit(via_url(url.rstrip("/")) if url else via_test_client())
