"""The training set: copies of every message the human labelled, sorted into two Maildirs, junk/ and keep/.

This is the only thing the model learns from and the only record of the human's judgment. Labelling copies the
message here, so refreshing or cleaning up the mail clone never changes it. An example's label is the folder its
copy is in: moving a file between junk/ and keep/ (by hand or in a mail client) relabels it.

index.tsv records where each label came from, one row per labelled message in the clone (folder + filename key).
plan uses it to apply the label to that exact message while it still exists, and review uses it to skip
messages already labelled. Several rows can share one copy when the same message (same Message-ID) was
delivered to several mailboxes."""

import csv
import hashlib
import os
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from .parse import open_message, split_name

LABELS = ("junk", "keep")
INDEX_FIELDS = ("example", "dir", "key", "user", "kind", "message_id", "labelled_at", "guess")
DEFAULT_KIND = "inbox"     # for copies added by hand, with no index row saying where they came from


class TrainingSet:
    def __init__(self, path):
        self.path = Path(path)
        self.index_path = self.path / "index.tsv"
        self.load()

    def load(self):
        self.files = {}          # example name -> (label, path to the copy)
        for label in LABELS:
            for sub in ("cur", "new"):
                d = self.path / label / sub
                if d.is_dir():
                    for f in sorted(d.iterdir()):
                        if f.is_file():
                            self.files[split_name(f.name)[0]] = (label, f)
        self.rows = []
        if self.index_path.exists():
            with open(self.index_path, newline="") as f:
                self.rows = list(csv.DictReader(f, delimiter="\t"))
        self.by_origin = {(r["dir"], r["key"]): r for r in self.rows}

    # --- reading ------------------------------------------------------------------------------

    def label_of(self, dir, key):
        r = self.by_origin.get((dir, key))
        hit = r and self.files.get(r["example"])
        return hit[0] if hit else None

    def counts(self) -> Counter:
        return Counter(label for label, _ in self.files.values())

    def examples(self):
        """(name, label, path, kind) for every copy, whatever the index says."""
        kinds = {}
        for r in self.rows:
            kinds.setdefault(r["example"], r["kind"] or DEFAULT_KIND)
        for name, (label, path) in sorted(self.files.items()):
            yield name, label, path, kinds.get(name, DEFAULT_KIND)

    def fingerprint(self) -> str:
        """Changes whenever an example is added, removed or relabelled."""
        h = hashlib.sha1()
        for name, (label, _) in sorted(self.files.items()):
            h.update(f"{name}\t{label}\n".encode())
        return h.hexdigest()[:12]

    def guesses(self):
        """(label, model's junk probability when it was labelled) for rows that had a model guess."""
        out = []
        for r in self.rows:
            label = self.label_of(r["dir"], r["key"])
            if label and r["guess"]:
                out.append((label, float(r["guess"])))
        return out

    def to_db(self, conn):
        """Mirror the labelled origins into a temporary table, so SQL can skip or join them."""
        ensure_table(conn)
        conn.execute("DELETE FROM temp.labelled")
        conn.executemany("INSERT OR REPLACE INTO temp.labelled VALUES (?, ?, ?)",
                         [(d, k, lab) for (d, k) in self.by_origin if (lab := self.label_of(d, k))])

    # --- writing ------------------------------------------------------------------------------

    def add(self, src, label, origin, guess=None, labelled_at=None):
        """Label the message at src. origin: dir, key, user, kind, message_id of the message in the clone."""
        assert label in LABELS, label
        mid = origin.get("message_id") or ""
        old = self.by_origin.get((origin["dir"], origin["key"]))
        if old and old["example"] in self.files:
            self.relabel(old["example"], label)
            return old["example"]
        shared = next((r["example"] for r in self.rows if mid and r["message_id"] == mid and r["example"] in self.files), None)
        if shared:
            self.relabel(shared, label)
            name = shared
        else:
            name = self._copy(src, label, origin["key"])
        row = {"example": name, "dir": origin["dir"], "key": origin["key"], "user": origin.get("user") or "",
               "kind": origin.get("kind") or DEFAULT_KIND, "message_id": mid,
               "labelled_at": labelled_at or datetime.now().isoformat(timespec="seconds"),
               "guess": "" if guess is None else f"{guess:.4f}"}
        if old:                  # its copy was deleted by hand; replace the stale row
            self.rows.remove(old)
            self._write_index()
        self.rows.append(row)
        self.by_origin[(row["dir"], row["key"])] = row
        new_file = not self.index_path.exists()
        with open(self.index_path, "a", newline="") as f:
            w = csv.DictWriter(f, INDEX_FIELDS, delimiter="\t")
            if new_file:
                w.writeheader()
            w.writerow(row)
        return name

    def relabel(self, name, label):
        cur, path = self.files[name]
        if cur != label:
            dst = self.path / label / "cur" / path.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dst)
            self.files[name] = (label, dst)

    def remove(self, dir, key):
        """Forget the label on one origin; delete its copy if no other origin uses it."""
        row = self.by_origin.pop((dir, key), None)
        if not row:
            return
        self.rows.remove(row)
        self._write_index()
        if not any(r["example"] == row["example"] for r in self.rows) and row["example"] in self.files:
            self.files.pop(row["example"])[1].unlink()

    def _copy(self, src, label, key):
        name, n = key, 1
        while name in self.files:
            n += 1
            name = f"{key}-{n}"
        flags = split_name(os.path.basename(src))[1]
        dst = self.path / label / "cur" / f"{name}:2,{flags}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        for sub in ("new", "tmp"):
            (self.path / label / sub).mkdir(exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        with open_message(src) as fin, open(tmp, "wb") as fout:   # stored uncompressed, so mail clients can read it
            shutil.copyfileobj(fin, fout)
        os.replace(tmp, dst)
        self.files[name] = (label, dst)
        return name

    def _write_index(self):
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, INDEX_FIELDS, delimiter="\t")
            w.writeheader()
            w.writerows(self.rows)
        os.replace(tmp, self.index_path)


def ensure_table(conn):
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS labelled (dir TEXT, key TEXT, label TEXT, PRIMARY KEY (dir, key))")


def import_legacy_labels(conn, training: TrainingSet, mail_root) -> tuple[int, list[str]]:
    """Move labels from the old inv.db `labels` table into the training set. The table is dropped only once
    every label has been copied. Returns (imported, problems)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='labels'").fetchone():
        return 0, []
    rows = conn.execute("""SELECT l.dir, l.key, l.label, l.guess, l.at, m.relpath, m.user, m.kind, m.message_id
                           FROM labels l LEFT JOIN messages m ON m.dir = l.dir AND m.key = l.key""").fetchall()
    done, problems = 0, []
    for r in rows:
        if training.label_of(r["dir"], r["key"]):
            continue
        src = os.path.join(mail_root, r["relpath"]) if r["relpath"] else None
        if not src or not os.path.exists(src):
            problems.append(f"{r['dir']}/{r['key']} ({r['label']}): message file not found")
            continue
        training.add(src, r["label"], dict(r), guess=r["guess"], labelled_at=(r["at"] or "").replace(" ", "T") or None)
        done += 1
    if not problems:
        conn.execute("DROP TABLE labels")
        conn.commit()
    return done, problems
