"""Message-level junk/keep classifier, trained only on the training set (training/junk and training/keep).

Each message becomes a bag of tokens: words from the subject and body, plus metadata tokens such as
the sender's domain and whether bulk-mail headers are present. Sender tokens let the model learn on its
own that everything from a domain is junk, without a rule fixing that up front."""

import json
import pickle
import re
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.utils import murmurhash3_32

from .cluster import ESP_HINTS, FREEMAIL
from .parse import extract_text, parse_message, split_name

N_FEATURES = 2**20
BODY_CHARS = 2000
_WORD = re.compile(r"[a-z][a-z0-9'&-]{1,29}|\$\d+|\d+%|<[a-z0-9.-]+>")

# Columns a document needs, from messages m joined with texts t.
DOC_COLUMNS = ("m.org, m.from_addr, m.kind, m.flags, m.list_unsub, m.bulk_hdrs, m.list_id, m.precedence, "
               "m.return_domain, m.spam_flag, m.subject, coalesce(t.text, '') AS text")


def meta_tokens(r) -> list[str]:
    org = r["org"] or "none"
    toks = [f"@org:{org}", f"@addr:{r['from_addr'] or 'none'}", f"@folder:{r['kind']}"]
    if org in FREEMAIL:
        toks.append("@freemail")
    if r["list_unsub"]:
        toks.append("@unsubscribe")
    if r["bulk_hdrs"]:
        toks.append("@bulk-headers")
    if r["list_id"]:
        toks.append("@mailing-list")
    if r["precedence"] in ("bulk", "list", "junk"):
        toks.append("@precedence-bulk")
    if any(h in (r["return_domain"] or "") for h in ESP_HINTS):
        toks.append("@bulk-sender-service")
    if r["spam_flag"]:
        toks.append("@spam-flagged")
    toks.append("@read" if "S" in (r["flags"] or "") else "@unread")
    return toks


def tokens(doc) -> list[str]:
    """doc is (meta_tokens, subject, body)."""
    meta, subject, body = doc
    subj = _WORD.findall((subject or "").lower())
    out = list(meta) * 2   # metadata is sparse but informative; count it twice
    out += [f"s:{w}" for w in subj]
    out += [f"s:{a}_{b}" for a, b in zip(subj, subj[1:])]
    out += _WORD.findall((body or "")[:BODY_CHARS].lower())
    return out


def make_doc(r):
    return (meta_tokens(r), r["subject"], r["text"])


_HEADER_FIELDS = ("org", "from_addr", "list_unsub", "bulk_hdrs", "list_id", "precedence", "return_domain", "spam_flag", "subject")


def example_doc(path, kind):
    """The same document make_doc builds from the inventory, read straight from a training copy."""
    h = parse_message(str(path))
    r = {k: h.get(k) for k in _HEADER_FIELDS}
    r.update(kind=kind, flags=split_name(Path(path).name)[1], text=extract_text(str(path)))
    return make_doc(r)


VECTORIZER = HashingVectorizer(n_features=N_FEATURES, analyzer=tokens, binary=True, norm="l2", alternate_sign=False)


def vectorize(docs):
    return VECTORIZER.transform(docs)


def _index(token: str) -> int:
    return abs(murmurhash3_32(token, seed=0)) % N_FEATURES


class Model:
    def __init__(self):
        self.clf = LogisticRegression(C=4.0, class_weight="balanced", max_iter=3000)

    def fit(self, X, y):
        self.clf.fit(X, y)
        self.junk_col = list(self.clf.classes_).index(1)
        return self

    def junk_prob(self, X) -> np.ndarray:
        return self.clf.predict_proba(X)[:, self.junk_col]

    def explain(self, doc, k=6):
        """Tokens of one message that push hardest toward junk (+) and keep (-)."""
        coef = self.clf.coef_[0] if self.junk_col == 1 else -self.clf.coef_[0]
        toks = set(tokens(doc))
        weights = sorted(((coef[_index(t)], t) for t in toks), reverse=True)
        junk = [(t, w) for w, t in weights[:k] if w > 0.05]
        keep = [(t, w) for w, t in weights[::-1][:k] if w < -0.05]
        return junk, keep

    def top_tokens(self, vocab, k=25):
        """Most junk-ish and keep-ish tokens among those seen in the labelled messages."""
        coef = self.clf.coef_[0] if self.junk_col == 1 else -self.clf.coef_[0]
        scored = sorted(((coef[_index(t)], t) for t in vocab), reverse=True)
        return scored[:k], scored[::-1][:k]


def labelled(conn, training):
    """(docs, y) for every example in the training set. y: 1 junk, 0 keep.

    Reading a copy takes ~10 ms, so documents are cached in inv.db (example_docs). The cache is derived:
    it is keyed by the copy's filename and folder kind, and rebuilt if missing."""
    cache = dict(conn.execute("SELECT example, doc FROM example_docs").fetchall())
    docs, y, new = [], [], []
    for _, label, path, kind in training.examples():
        ck = f"{path.name}\t{kind}"
        if ck in cache:
            doc = json.loads(cache[ck])
        else:
            doc = example_doc(path, kind)
            new.append((ck, json.dumps(doc)))
        docs.append(doc)
        y.append(1 if label == "junk" else 0)
    if new:
        conn.executemany("INSERT OR REPLACE INTO example_docs(example, doc) VALUES (?, ?)", new)
        conn.commit()
    return docs, np.array(y, dtype=int)


def can_train(y) -> bool:
    return len(y) >= 4 and 0 < y.sum() < len(y)


def train(conn, training) -> Model | None:
    docs, y = labelled(conn, training)
    if not can_train(y):
        return None
    return Model().fit(vectorize(docs), y)


def evaluate(conn, training) -> dict:
    """Cross-validated accuracy on the training set, and what the model has learned."""
    docs, y = labelled(conn, training)
    rep = {"examples": len(y), "junk": int(y.sum()), "keep": int(len(y) - y.sum())}
    if not can_train(y):
        rep["error"] = "label at least a few junk and a few keep messages first"
        return rep
    X = vectorize(docs)
    folds = int(min(5, y.sum(), len(y) - y.sum()))
    if folds >= 2:
        pred = cross_val_predict(Model().clf, X, y, cv=StratifiedKFold(folds, shuffle=True, random_state=0))
        rep["cv_accuracy"] = float((pred == y).mean())
        rep["junk_called_keep"] = int(((y == 1) & (pred == 0)).sum())
        rep["keep_called_junk"] = int(((y == 0) & (pred == 1)).sum())
    guesses = training.guesses()
    if guesses:
        agree = sum((g >= 0.5) == (label == "junk") for label, g in guesses)
        rep["live_agreement"] = f"{agree}/{len(guesses)} labels matched the model's guess at the time"
    model = Model().fit(X, y)
    vocab = {t for d in docs for t in tokens(d)}
    rep["junk_tokens"], rep["keep_tokens"] = model.top_tokens(vocab)
    return rep


# --- versions: every distinct training set gets its own saved model; nothing is overwritten -------------

def latest_version(models_dir) -> tuple[Path, dict] | None:
    metas = sorted(Path(models_dir).glob("model-*.json"), key=lambda f: (f.stat().st_mtime_ns, f.name))
    return (metas[-1].with_suffix(".pkl"), json.loads(metas[-1].read_text())) if metas else None


def load(path) -> Model:
    return pickle.loads(Path(path).read_bytes())


def save_version(model: Model, models_dir, training, extra=None) -> tuple[Path, bool]:
    """Save model-YYYYMMDD-HHMMSS.pkl + .json unless the latest saved model has the same training set.
    Returns (path, saved)."""
    fp = training.fingerprint()
    latest = latest_version(models_dir)
    if latest and latest[1].get("training") == fp:
        return latest[0], False
    d = Path(models_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    path, n = d / f"model-{stamp}.pkl", 1
    while path.exists():
        n += 1
        path = d / f"model-{stamp}-{n}.pkl"
    n = training.counts()
    meta = {"created": datetime.now().isoformat(timespec="seconds"), "training": fp,
            "examples": sum(n.values()), "junk": n["junk"], "keep": n["keep"], **(extra or {})}
    path.write_bytes(pickle.dumps(model))
    path.with_suffix(".json").write_text(json.dumps(meta, indent=1) + "\n")
    return path, True
