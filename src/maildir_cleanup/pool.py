"""Choose which messages the human labels next.

Works on a random sample of the mail that is actually up for deletion: older than the cutoff, not
protected, and not already handled by folder rules (Junk, Trash, server spam flag)."""

import random
from collections import Counter, defaultdict

import numpy as np

from . import model as ml
from .training import ensure_table

ELIGIBLE = """m.kind NOT IN ('sent','drafts','junk','trash') AND m.protect IS NULL AND m.ts < ?
              AND coalesce(m.spam_flag, 0) = 0
              AND NOT EXISTS (SELECT 1 FROM temp.labelled l WHERE l.dir = m.dir AND l.key = m.key)"""

# Where a --grep keyword may appear: the stored headers and the extracted body text.
SEARCHED = ("m.subject", "m.from_addr", "m.from_name", "m.to_addrs", "m.list_id", "m.return_domain", "t.text")


class Pool:
    def __init__(self, conn, cutoff_ts, size=20000, user=None, seed=None, grep=()):
        """grep: keywords that must all appear (case-insensitive) in the headers or body text.
        Messages already in the training set are skipped (TrainingSet.to_db fills temp.labelled)."""
        ensure_table(conn)
        self.rng = random.Random(seed)
        where, params = ELIGIBLE, [cutoff_ts]
        if user:
            where += " AND m.user = ?"
            params.append(user)
        for word in grep:
            where += " AND (" + " OR ".join(f"{c} LIKE ?" for c in SEARCHED) + ")"
            params += [f"%{word}%"] * len(SEARCHED)
        src = "messages m LEFT JOIN texts t ON t.message = m.id"
        tot = conn.execute(f"SELECT count(*), coalesce(sum(m.size), 0) FROM {src} WHERE {where}", params).fetchone()
        self.eligible_n, self.eligible_bytes = tot[0], tot[1]
        self.org_total = Counter({r[0]: r[1] for r in conn.execute(
            f"SELECT m.org, count(*) FROM {src} WHERE {where} GROUP BY m.org", params)})
        self.rows = conn.execute(f"""SELECT m.id, m.dir, m.key, m.user, m.folder, m.relpath, m.ts, m.size, m.from_name, m.message_id,
                                            {ml.DOC_COLUMNS}
                                     FROM {src} WHERE {where} ORDER BY random() LIMIT ?""", (*params, size)).fetchall()
        self.docs = [ml.make_doc(r) for r in self.rows]
        self.X = ml.vectorize(self.docs) if self.rows else None
        self.by_org = defaultdict(list)
        for i, r in enumerate(self.rows):
            self.by_org[r["org"]].append(i)
        self.p = None
        self.done = set()       # pool indexes labelled or already shown
        self.shown_orgs = Counter()

    def score(self, model):
        self.p = model.junk_prob(self.X) if (model and self.X is not None) else None

    def page(self, n=15, threshold=0.85) -> list[int]:
        free = [i for i in range(len(self.rows)) if i not in self.done]
        if not free:
            return []
        picks = self._uncertain(free, n, threshold) if self.p is not None else self._diverse(free, n)
        self.done.update(picks)
        for i in picks:
            self.shown_orgs[self.rows[i]["org"]] += 1
        return picks

    def _diverse(self, free, n):
        """Before there is a model: one message each from the senders with the most mail, rotating."""
        free_set = set(free)
        orgs = sorted(self.by_org, key=lambda o: -self.org_total[o] / (1 + self.shown_orgs[o]) ** 2)
        picks = []
        for org in orgs:
            options = [i for i in self.by_org[org] if i in free_set]
            if options:
                picks.append(self.rng.choice(options))
            if len(picks) >= n:
                break
        return picks

    def _uncertain(self, free, n, threshold):
        """Mostly what the model is least sure of (at most 2 per sender), plus spot checks of mail it would
        delete (score at or above the threshold) and mail it is sure to keep."""
        p = self.p
        free = np.array(free)
        order = free[np.argsort(np.abs(p[free] - 0.5))]
        picks, per_org = [], Counter()
        for i in order:
            org = self.rows[i]["org"]
            if per_org[org] < 2:
                picks.append(int(i))
                per_org[org] += 1
            if len(picks) >= n - 4:
                break
        rest = [int(i) for i in free if int(i) not in set(picks)]
        sure_junk = [i for i in rest if p[i] >= threshold]
        sure_keep = [i for i in rest if p[i] <= 0.1]
        for group in (sure_junk, sure_keep):
            picks += self.rng.sample(group, min(2, len(group)))
        if len(picks) < n:
            picks += [i for i in rest if i not in set(picks)][: n - len(picks)]
        return picks

    def estimate(self, threshold):
        """Estimated (messages, bytes) of eligible mail the model would delete at this threshold."""
        if self.p is None or not self.rows:
            return None
        sizes = np.array([r["size"] or 0 for r in self.rows], dtype=float)
        hit = self.p >= threshold
        return (int(hit.mean() * self.eligible_n), int(sizes[hit].sum() / max(sizes.sum(), 1) * self.eligible_bytes))

    def org_view(self, org):
        """(eligible messages from this sender domain, sampled count, share the model calls junk)."""
        idx = self.by_org.get(org, [])
        share = float((self.p[idx] >= 0.5).mean()) if (self.p is not None and idx) else None
        return self.org_total[org], len(idx), share
