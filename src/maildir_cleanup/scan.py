"""Walk the mail root and record every message's headers in the inventory. Read-only on mail."""

import multiprocessing
import os
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

from .db import MESSAGE_FIELDS
from .parse import parse_message, split_name

BATCH = 400

_KINDS = (
    ("inbox", re.compile(r"^inbox$", re.I)),
    ("sent", re.compile(r"^(sent|sent[ ._-]?(items|mail|messages)|outbox)$", re.I)),
    ("drafts", re.compile(r"^drafts?$", re.I)),
    ("junk", re.compile(r"^(junk|spam|junk[ ._-]?e-?mail|bulk[ ._-]?mail)$", re.I)),
    ("trash", re.compile(r"^(trash|deleted[ ._-]?(items|messages)?|bin)$", re.I)),
)


def folder_kind(folder: str) -> str:
    if folder == "INBOX":
        return "inbox"
    leaf = re.split(r"[./]", folder)[-1]
    for kind, rx in _KINDS:
        if rx.match(leaf):
            return kind
    return "other"


def find_users(root: str, max_depth: int = 3):
    """Yield (user, maildir_root) for each mailbox under root.

    A mailbox is a directory holding a Maildir/ subdirectory, or a directory that is itself a
    Maildir (has cur/ and new/). The user name is its path below root, so both root/alice and
    a virtual-domain layout like root/example.com/alice/Maildir work."""

    def walk(path, depth):
        if os.path.isdir(os.path.join(path, "Maildir")):
            yield os.path.relpath(path, root), os.path.join(path, "Maildir")
            return
        if depth > 0 and os.path.isdir(os.path.join(path, "cur")) and os.path.isdir(os.path.join(path, "new")):
            yield os.path.relpath(path, root), path
            return
        if depth >= max_depth:
            return
        for entry in sorted(os.scandir(path), key=lambda e: e.name):
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                yield from walk(entry.path, depth + 1)

    yield from walk(root, 0)


def iter_folders(mroot: str):
    """Yield (folder_dir, display_name) for every directory that holds cur/ or new/."""
    for dirpath, dirnames, _ in os.walk(mroot):
        if "cur" in dirnames or "new" in dirnames:
            rel = os.path.relpath(dirpath, mroot)
            if rel == ".":
                name = "INBOX"
            else:
                name = "/".join(p[1:] if p.startswith(".") else p for p in rel.split(os.sep))
            yield dirpath, name
        dirnames[:] = sorted(d for d in dirnames if d not in ("cur", "new", "tmp"))


def iter_messages(root: str, users: list[str] | None = None):
    for user, mroot in find_users(root):
        if users and user not in users:
            continue
        for fdir, folder in iter_folders(mroot):
            for sub in ("cur", "new"):
                d = os.path.join(fdir, sub)
                if not os.path.isdir(d):
                    continue
                for entry in os.scandir(d):
                    if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                        continue
                    yield {
                        "user": user,
                        "folder": folder,
                        "kind": folder_kind(folder),
                        "dir": os.path.relpath(fdir, root),
                        "sub": sub,
                        "name": entry.name,
                        "relpath": os.path.relpath(entry.path, root),
                    }


def _parse_batch(root: str, items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        key, flags = split_name(item["name"])
        row = dict(item, key=key, flags=flags)
        try:
            row.update(parse_message(os.path.join(root, item["relpath"])))
        except OSError as e:  # vanished or unreadable
            row.update(size=None, ts=None, error=f"{type(e).__name__}: {e}")
        out.append(row)
    return out


def _batches(it, n):
    batch = []
    for x in it:
        batch.append(x)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def scan(conn, root: str, workers: int = 0, users: list[str] | None = None, progress=True) -> dict:
    """Add new messages, drop rows for files that no longer exist. Resumable."""
    root = os.path.abspath(root)
    known = {r[0] for r in conn.execute("SELECT relpath FROM messages")}
    seen = set()
    pending = (m for m in iter_messages(root, users) if not (seen.add(m["relpath"]) or m["relpath"] in known))

    cols = ", ".join(MESSAGE_FIELDS)
    marks = ", ".join("?" for _ in MESSAGE_FIELDS)
    sql = f"INSERT OR REPLACE INTO messages ({cols}) VALUES ({marks})"
    added = 0
    started = shown = time.time()
    workers = workers or os.cpu_count() or 2

    def store(rows):
        nonlocal added, shown
        conn.executemany(sql, [tuple(r.get(f) for f in MESSAGE_FIELDS) for r in rows])
        conn.commit()
        added += len(rows)
        if progress and time.time() - shown > 1:
            shown = time.time()
            rate = added / max(time.time() - started, 1e-6)
            print(f"\r  {added:,} new messages ({rate:,.0f}/s)", end="", file=sys.stderr, flush=True)

    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("forkserver")) as pool:
        inflight = set()
        for batch in _batches(pending, BATCH):
            inflight.add(pool.submit(_parse_batch, root, batch))
            if len(inflight) >= workers * 4:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for f in done:
                    store(f.result())
        for f in inflight:
            store(f.result())
    if progress and added:
        print(file=sys.stderr)

    gone = known - seen
    if users:
        gone = {p for p in gone if any(p.startswith(u + os.sep) for u in users)}
    for table in ("plan", "texts"):
        conn.executemany(f"DELETE FROM {table} WHERE message IN (SELECT id FROM messages WHERE relpath=?)", ((p,) for p in gone))
    conn.executemany("DELETE FROM messages WHERE relpath=?", ((p,) for p in gone))
    conn.commit()
    return {"added": added, "removed": len(gone), "total": conn.execute("SELECT count(*) FROM messages").fetchone()[0]}
