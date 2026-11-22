"""Coverage-index compaction.

After the same stretch of road is re-surveyed many times, ``coverage_rects``
accumulates rows whose rectangle is already fully covered by higher-version
rectangles — either by a single newer rectangle or by several newer ones
tiled together.  Box queries keep dragging those dead rows along.  This
module removes them.

What is preserved (and why queries cannot tell the difference)
--------------------------------------------------------------
Only ``coverage_rects`` rows are deleted.  ``packages`` (records and
digests) and ``current_wet`` are never touched.  A rect R (version v) is
removed only when R is contained in the union of *remaining* rects with
version > v, which keeps every observable identical:

* surveyed range — it is the union of all rects; R adds nothing to it;
* per-cell ``dry`` version — the highest version covering a cell is never
  removed: its own remover would be a higher rect covering the same cell;
* wet cells and their versions — ``current_wet`` is not modified;
* late-arriving old packages — ingest shadows against the union of rects
  newer than the incoming version; that union is unchanged for every
  possible incoming version, because any removed rect with version > p is
  covered by remaining rects with version > v > p.

Redundancy is monotone: importing new packages can only add coverage, so a
removal planned from a slightly stale snapshot is still valid when it lands.
The one operation that invalidates a plan is ``/api/reset``, which bumps the
``meta`` epoch; the delete transaction re-checks the epoch and abandons the
round if it moved — even if identical version numbers were imported again.

Locking
-------
The read phase uses plain autocommit SELECTs (WAL readers never take the
write lock) and the rectangle math runs in Python with no transaction open,
so computing coverage never holds the SQLite write lock.  Only the short
delete runs inside ``BEGIN IMMEDIATE``; any failure there rolls the whole
round back.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .repository import _transaction


@dataclass(frozen=True)
class Rect:
    version: int
    r0: int
    r1: int
    c0: int
    c1: int


class StaleCompactionError(Exception):
    """The epoch moved while coverage was being computed (demo reset)."""


def _read_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
    return 0 if row is None else row["value"]


def read_snapshot(conn: sqlite3.Connection) -> tuple[int, list[Rect]]:
    """Epoch and current rectangles, read without taking the write lock.

    The epoch is read *first*: any reset between the two SELECTs moves the
    epoch, and the delete phase will refuse to commit this round.
    """
    epoch = _read_epoch(conn)
    rows = conn.execute(
        "SELECT version, r0, r1, c0, c1 FROM coverage_rects ORDER BY version"
    ).fetchall()
    rects = [Rect(r["version"], r["r0"], r["r1"], r["c0"], r["c1"]) for r in rows]
    return epoch, rects


def _intervals_cover(intervals: list[tuple[int, int]], lo: int, hi: int) -> bool:
    """True iff the union of ``intervals`` covers the whole of ``[lo, hi)``."""
    cur = lo
    for start, end in sorted(intervals):
        if start > cur:
            return False
        if end > cur:
            cur = end
            if cur >= hi:
                return True
    return cur >= hi


def _covered_by_union(rect: Rect, higher: list[Rect]) -> bool:
    """True iff ``rect`` is fully covered by the union of ``higher`` rects.

    Pure rectangle math, no per-cell expansion: sweep the row axis.  Between
    two consecutive row boundaries the set of overlapping higher rects is
    constant, so 2-D coverage reduces to a 1-D interval-cover check on
    columns for each row slab.
    """
    relevant: list[tuple[int, int, int, int]] = []
    bounds = {rect.r0, rect.r1}
    for h in higher:
        if h.r0 < rect.r1 and h.r1 > rect.r0 and h.c0 < rect.c1 and h.c1 > rect.c0:
            a0 = max(h.r0, rect.r0)
            a1 = min(h.r1, rect.r1)
            relevant.append((a0, a1, h.c0, h.c1))
            bounds.add(a0)
            bounds.add(a1)
    if not relevant:
        return False
    rows = sorted(bounds)
    for lo, hi in zip(rows, rows[1:]):
        if lo >= hi:
            continue
        intervals = [(c0, c1) for a0, a1, c0, c1 in relevant if a0 <= lo and a1 >= hi]
        if not _intervals_cover(intervals, rect.c0, rect.c1):
            return False
    return True


def find_redundant(rects: list[Rect]) -> list[int]:
    """Versions whose rect is covered by the union of higher-version rects.

    Rects are considered against *all* higher rects, including ones that are
    themselves redundant.  Removing the whole set at once is still safe:
    induction over descending version shows every removed rect stays covered
    by higher rects that survive.
    """
    redundant: list[int] = []
    higher: list[Rect] = []
    for rect in sorted(rects, key=lambda r: r.version, reverse=True):
        if higher and _covered_by_union(rect, higher):
            redundant.append(rect.version)
        higher.append(rect)
    return redundant


def apply_compaction(
    conn: sqlite3.Connection, epoch: int, versions: list[int]
) -> int:
    """Delete ``versions`` from ``coverage_rects`` iff ``epoch`` is current.

    The epoch check and the delete share one ``BEGIN IMMEDIATE``
    transaction, so no reset can slip between them.  Any failure rolls the
    whole round back.  Raises :class:`StaleCompactionError` when the epoch
    moved (a reset happened while coverage was being computed).
    """
    with _transaction(conn):
        if _read_epoch(conn) != epoch:
            raise StaleCompactionError(
                "压缩期间演示库被清空，本轮压缩已作废，未删除任何索引项。"
            )
        conn.executemany(
            "DELETE FROM coverage_rects WHERE version = ?",
            [(v,) for v in versions],
        )
    return len(versions)


def compact_coverage(conn: sqlite3.Connection) -> dict:
    """Plan and apply one compaction round; returns the API payload."""
    started = time.perf_counter()
    epoch, rects = read_snapshot(conn)
    redundant = find_redundant(rects)
    base = {
        "rects_before": len(rects),
        "candidates": len(redundant),
    }
    if not redundant:
        base.update(
            status="noop",
            removed_versions=[],
            removed_count=0,
            rects_after=len(rects),
            message="没有被更高版矩形完全盖住的索引项，无需压缩。",
        )
        base["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return base
    try:
        apply_compaction(conn, epoch, redundant)
    except StaleCompactionError as exc:
        base.update(
            status="stale",
            removed_versions=[],
            removed_count=0,
            rects_after=len(rects),
            message=str(exc),
        )
        base["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return base
    removed = sorted(redundant)
    base.update(
        status="compacted",
        removed_versions=removed,
        removed_count=len(removed),
        rects_after=len(rects) - len(removed),
        message=(
            f"已移除 {len(removed)} 个被更高版矩形完全盖住的索引项"
            f"（版号 {', '.join(map(str, removed))}）；包记录与摘要保留。"
        ),
    )
    base["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return base
