"""Store the readable body text of each message, for the classifier and the review preview."""

import multiprocessing
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

from .parse import extract_text

BATCH = 200


def _extract_batch(root, items):
    return [(mid, extract_text(os.path.join(root, rel))) for mid, rel in items]


def extract(conn, root: str, workers: int = 0, progress=True) -> int:
    """Extract text for every received message that has none yet. Resumable."""
    todo = conn.execute("""SELECT id, relpath FROM messages
                           WHERE kind NOT IN ('sent','drafts') AND id NOT IN (SELECT message FROM texts)""").fetchall()
    total = len(todo)
    workers = workers or os.cpu_count() or 2
    done = 0
    started = shown = time.time()

    def store(rows):
        nonlocal done, shown
        conn.executemany("INSERT OR REPLACE INTO texts(message, text) VALUES (?, ?)", rows)
        conn.commit()
        done += len(rows)
        if progress and time.time() - shown > 1:
            shown = time.time()
            rate = done / max(time.time() - started, 1e-6)
            eta = (total - done) / max(rate, 1e-6)
            print(f"\r  text {done:,}/{total:,} ({rate:,.0f}/s, ~{eta / 60:.0f} min left)   ",
                  end="", file=sys.stderr, flush=True)

    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("forkserver")) as pool:
        inflight = set()
        for i in range(0, total, BATCH):
            items = [(r[0], r[1]) for r in todo[i:i + BATCH]]
            inflight.add(pool.submit(_extract_batch, root, items))
            if len(inflight) >= workers * 4:
                finished, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for f in finished:
                    store(f.result())
        for f in inflight:
            store(f.result())
    if progress and total:
        print(file=sys.stderr)
    return done
