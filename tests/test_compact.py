from __future__ import annotations

import random
import sqlite3

import pytest

from app.compact import (
    Rect,
    StaleCompactionError,
    apply_compaction,
    compact_coverage,
    find_redundant,
    read_snapshot,
)
from app.db import connect, init_db
from app.parsing import parse_package
from app.repository import ingest

from .conftest import cell, make_app, post_pkg, wet
from .test_parsing import pkg


@pytest.fixture()
def client(tmp_path):
    app = make_app(tmp_path)
    with app.test_client() as c:
        yield c


def compact(client):
    response = client.post("/api/compact")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def cell_grid(client, r0, r1, c0, c1):
    """Full per-cell verdicts; expansion is fine in tests (never in app code)."""
    return {
        (r, c): (cell(client, r, c)["status"], cell(client, r, c)["version"])
        for r in range(r0, r1)
        for c in range(c0, c1)
    }


# --------------------------------------------------------------------------
# rectangle math
# --------------------------------------------------------------------------

def R(version, r0, r1, c0, c1):
    return Rect(version, r0, r1, c0, c1)


def test_single_higher_rect_covers():
    rects = [R(1, 0, 10, 0, 10), R(2, 0, 20, 0, 20)]
    assert find_redundant(rects) == [1]


def test_union_of_higher_rects_tiles_older():
    # Neither v2 nor v3 covers v1 alone; together they tile it exactly.
    rects = [R(1, 0, 10, 0, 10), R(2, 0, 10, 0, 5), R(3, 0, 10, 5, 10)]
    assert find_redundant(rects) == [1]


def test_partial_overlap_is_kept():
    rects = [R(1, 0, 10, 0, 10), R(2, 5, 15, 5, 15)]
    assert find_redundant(rects) == []


def test_gap_in_union_is_kept():
    rects = [R(1, 0, 10, 0, 10), R(2, 0, 10, 0, 4), R(3, 0, 10, 5, 10)]
    assert find_redundant(rects) == []


def test_nested_chain_all_removed():
    rects = [R(1, 0, 5, 0, 5), R(2, 0, 10, 0, 10), R(3, 0, 20, 0, 20)]
    assert find_redundant(rects) == [2, 1]


def test_transitive_cover_through_redundant_rect():
    # v1 is only covered by v2, and v2 only by v3: both may go in one round.
    rects = [R(1, 0, 4, 0, 4), R(2, 0, 8, 0, 8), R(3, 0, 16, 0, 16)]
    assert find_redundant(rects) == [2, 1]


def test_identical_rects_keep_highest_version():
    rects = [R(1, 0, 10, 0, 10), R(2, 0, 10, 0, 10), R(3, 0, 10, 0, 10)]
    assert sorted(find_redundant(rects)) == [1, 2]


def test_negative_coordinates():
    rects = [R(1, -10, 10, -10, 10), R(2, -20, 20, -20, 0), R(3, -20, 20, 0, 20)]
    assert find_redundant(rects) == [1]


def test_find_redundant_matches_bruteforce():
    rng = random.Random(20261122)
    for _ in range(300):
        n = rng.randint(1, 8)
        rects = []
        for version in range(1, n + 1):
            r0 = rng.randint(0, 7)
            r1 = r0 + rng.randint(1, 8)
            c0 = rng.randint(0, 7)
            c1 = c0 + rng.randint(1, 8)
            rects.append(R(version, r0, r1, c0, c1))
        expected = []
        for rect in rects:
            higher = [h for h in rects if h.version > rect.version]
            covered = all(
                any(h.r0 <= r < h.r1 and h.c0 <= c < h.c1 for h in higher)
                for r in range(rect.r0, rect.r1)
                for c in range(rect.c0, rect.c1)
            )
            if covered:
                expected.append(rect.version)
        assert sorted(find_redundant(rects)) == expected


# --------------------------------------------------------------------------
# end-to-end behaviour through the app
# --------------------------------------------------------------------------

def test_compact_removes_covered_rect_and_keeps_packages(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(2, 2)]))
    post_pkg(client, pkg(2, 0, 20, 0, 20, [(7, 7)]))
    before = client.get("/api/packages").get_json()
    assert before["coverage_rect_count"] == 2

    result = compact(client)
    assert result["status"] == "compacted"
    assert result["removed_versions"] == [1]
    assert result["rects_before"] == 2
    assert result["rects_after"] == 1

    after = client.get("/api/packages").get_json()
    # Package records and digests are untouched; only the index shrank.
    assert [(p["version"], p["digest"]) for p in after["packages"]] == [
        (p["version"], p["digest"]) for p in before["packages"]
    ]
    assert after["coverage_rect_count"] == 1
    # Box query only sees the surviving rectangle now.
    result = wet(client, 0, 20, 0, 20)
    assert [r["version"] for r in result["coverage_rects"]] == [2]
    # (2,2) was superseded by v2's dry resweep at ingest time; (7,7) stays.
    assert {(c["row"], c["col"]) for c in result["cells"]} == {(7, 7)}
    assert cell(client, 2, 2) == {
        "row": 2,
        "col": 2,
        "status": "dry",
        "version": 2,
        "label": "已查无积水",
    }


def test_compact_observables_unchanged(client):
    # A layered history: overlaps, a late arrival, a resweep that dries out.
    post_pkg(client, pkg(1, 0, 12, 0, 12, [(1, 1), (5, 5)]))
    post_pkg(client, pkg(2, 4, 16, 4, 16, [(8, 8)]))
    post_pkg(client, pkg(3, 0, 12, 0, 12, []))          # resweep: v1 area dries
    post_pkg(client, pkg(2, 4, 16, 4, 16, [(8, 8)]))   # same-content retry
    post_pkg(client, pkg(5, 10, 20, 0, 10, [(15, 2)]))
    post_pkg(client, pkg(4, 0, 20, 0, 20, [(19, 19)])) # late: v4 < v5
    before_cells = cell_grid(client, -1, 21, -1, 21)
    before_wet = wet(client, 0, 20, 0, 20)["cells"]
    before_packages = client.get("/api/packages").get_json()["packages"]

    result = compact(client)
    assert result["status"] == "compacted"
    # v1..v3 are covered by the union of higher rects; v4 (the largest rect)
    # and v5 survive.
    assert result["removed_versions"] == [1, 2, 3]

    assert cell_grid(client, -1, 21, -1, 21) == before_cells
    assert wet(client, 0, 20, 0, 20)["cells"] == before_wet
    after_packages = client.get("/api/packages").get_json()["packages"]
    assert [(p["version"], p["digest"]) for p in after_packages] == [
        (p["version"], p["digest"]) for p in before_packages
    ]
    # Box query now paints with the surviving rectangles only.
    assert [r["version"] for r in wet(client, 0, 20, 0, 20)["coverage_rects"]] == [4, 5]


def test_late_package_after_compact_still_judged_by_highest_version(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(2, 2)]))
    post_pkg(client, pkg(8, 0, 10, 0, 10, []))  # v8 resweeps: all dry
    assert compact(client)["removed_versions"] == [1]

    # A late v3 must still be shadowed by v8 everywhere in the rectangle,
    # exactly as if v1's rect were still in the index.
    response = post_pkg(client, pkg(3, 0, 10, 0, 10, [(7, 7)]))
    summary = response.get_json()["summary"]
    assert summary["current_wet_added"] == 0
    assert summary["wet_points_shadowed_by_newer"] == 1
    assert cell(client, 7, 7)["status"] == "dry"
    assert cell(client, 7, 7)["version"] == 8

    # The late v3 rect is itself covered by v8: a second round removes it.
    assert compact(client)["removed_versions"] == [3]


def test_compact_noop_when_nothing_redundant(client):
    assert compact(client)["status"] == "noop"
    post_pkg(client, pkg(1, 0, 10, 0, 10, []))
    post_pkg(client, pkg(2, 5, 15, 5, 15, []))
    result = compact(client)
    assert result["status"] == "noop"
    assert result["rects_after"] == 2


def test_compact_works_without_rtree(tmp_path):
    app = make_app(tmp_path, PREFER_RTREE=False)
    with app.test_client() as c:
        post_pkg(c, pkg(1, 0, 10, 0, 10, [(1, 1)]))
        post_pkg(c, pkg(2, 0, 10, 0, 10, []))
        result = compact(c)
        assert result["status"] == "compacted"
        assert result["removed_versions"] == [1]
        assert cell(c, 1, 1)["status"] == "dry"
        assert cell(c, 1, 1)["version"] == 2


# --------------------------------------------------------------------------
# concurrency contract
# --------------------------------------------------------------------------

def _file_db(tmp_path):
    path = str(tmp_path / "compact.sqlite3")
    conn = connect(path)
    init_db(conn)
    return conn


def test_compute_phase_does_not_hold_write_lock(tmp_path):
    """A second connection must be able to import while a round is planned."""
    conn = _file_db(tmp_path)
    ingest(conn, parse_package(pkg(1, 0, 10, 0, 10, [(1, 1)])))
    ingest(conn, parse_package(pkg(2, 0, 10, 0, 10, [])))

    epoch, rects = read_snapshot(conn)
    redundant = find_redundant(rects)
    assert redundant == [1]

    # While the first connection "is still computing", another connection
    # writes a new package.  If the read/compute phase held the write lock
    # this would block and fail with 'database is locked'.
    other = connect(conn.execute("PRAGMA database_list").fetchone()[2])
    try:
        result = ingest(other, parse_package(pkg(3, 20, 30, 20, 30, [(25, 25)])))
        assert result["status"] == "imported"
    finally:
        other.close()

    # The planned removal is still valid: new packages only add coverage.
    assert apply_compaction(conn, epoch, redundant) == 1
    remaining = {
        r[0] for r in conn.execute("SELECT version FROM coverage_rects").fetchall()
    }
    assert remaining == {2, 3}
    conn.close()


def test_reset_during_compact_voids_round(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))
    post_pkg(client, pkg(2, 0, 10, 0, 10, []))

    from app.db import get_db  # noqa: PLC0415

    # Plan a round against the current snapshot.
    with client.application.test_request_context():
        conn = get_db()
        epoch, rects = read_snapshot(conn)
        redundant = find_redundant(rects)
        assert redundant == [1]

    # The demo library is wiped while the round is being computed...
    assert client.post("/api/reset").status_code == 200
    # ...and the very same version numbers are imported again.
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))
    post_pkg(client, pkg(2, 0, 10, 0, 10, []))

    with client.application.test_request_context():
        conn = get_db()
        with pytest.raises(StaleCompactionError):
            apply_compaction(conn, epoch, redundant)
        # Nothing was deleted: the reused version numbers are not the rects
        # the plan was computed from.
        assert conn.execute(
            "SELECT COUNT(*) FROM coverage_rects"
        ).fetchone()[0] == 2
        # A fresh round works on the new data.
        assert compact_coverage(conn)["status"] == "compacted"


def test_compact_write_failure_rolls_back(tmp_path):
    conn = _file_db(tmp_path)
    ingest(conn, parse_package(pkg(1, 0, 10, 0, 10, [(1, 1)])))
    ingest(conn, parse_package(pkg(2, 0, 10, 0, 10, [])))
    epoch, rects = read_snapshot(conn)
    redundant = find_redundant(rects)

    class FailingConn:
        def __getattr__(self, name):
            return getattr(conn, name)

        def executemany(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        apply_compaction(FailingConn(), epoch, redundant)

    # The failed round left everything exactly as it was.
    assert conn.execute("SELECT COUNT(*) FROM coverage_rects").fetchone()[0] == 2
    # And the database is not wedged: a normal round succeeds afterwards.
    result = compact_coverage(conn)
    assert result["status"] == "compacted"
    assert result["removed_versions"] == [1]
    conn.close()


def test_two_compactions_in_a_row_are_safe(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, []))
    post_pkg(client, pkg(2, 0, 10, 0, 10, []))
    assert compact(client)["removed_versions"] == [1]
    assert compact(client)["status"] == "noop"
    assert cell(client, 5, 5)["version"] == 2
