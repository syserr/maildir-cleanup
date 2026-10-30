"""Decide which messages to delete, write a markdown report, and export deletion lists."""

import os
from collections import Counter
from datetime import date, datetime, timezone

from . import model as ml

BATCH = 5000
SCORE_STEPS = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)


def decide(r, rules, cfg, cutoff_ts, p, threshold):
    """Return the deletion reason for one message, or None to keep it.

    Precedence: protection signals, then rules.toml overrides, then the human's own label on this
    message, then the model's junk probability p, then folder cleanup (Junk, Trash, spam flag)."""
    if r["kind"] in ("sent", "drafts"):
        return None
    old = (r["ts"] or 0) < cutoff_ts
    rule = rules.resolve(r["user"], r["from_addr"], r["from_domain"], r["list_id"])
    if (rule and rule.action == "protect") or r["protect"]:
        return None
    decided_keep = False
    if rule:
        if rule.action == "spam":
            return f"rule: {rule.describe()}"
        if rule.action == "ad" and old:
            return f"rule: {rule.describe()}"
        decided_keep = rule.action == "keep"
    elif r["label"] == "junk":
        if old:
            return "label: junk"
    elif r["label"] == "keep":
        decided_keep = True
    elif p is not None and p >= threshold and old:
        return f"model: {p:.2f}"
    # Folder cleanup applies even to senders judged worth keeping: someone already threw these away.
    if r["kind"] == "junk" and (cfg.junk_any_age or old):
        return "folder: junk"
    if r["kind"] == "trash" and cfg.trash_before_cutoff and old:
        return "folder: trash"
    if r["spam_flag"] and cfg.spam_header_before_cutoff and old and not decided_keep:
        return "spam header: server flagged"
    return None


def build_plan(conn, training, rules, cfg, cutoff_ts, threshold, models_dir=None) -> dict:
    """Recompute the plan table. Returns tallies for the report.
    With models_dir, the model is saved there as a new version if the training set changed."""
    training.to_db(conn)
    model = ml.train(conn, training)
    model_file = ml.save_version(model, models_dir, training)[0] if (model and models_dir) else None
    missing = conn.execute("""SELECT count(*) FROM messages WHERE kind NOT IN ('sent','drafts')
                              AND id NOT IN (SELECT message FROM texts)""").fetchone()[0]
    conn.execute("DELETE FROM plan")
    tally, borderline = Counter(), Counter()
    scored = []    # (p, size) of mail the model alone decides, for the threshold table
    cur = conn.execute("""SELECT m.*, coalesce(t.text, '') AS text, l.label FROM messages m
                          LEFT JOIN texts t ON t.message = m.id
                          LEFT JOIN temp.labelled l ON l.dir = m.dir AND l.key = m.key""")
    out = conn.cursor()
    while batch := cur.fetchmany(BATCH):
        need = [i for i, r in enumerate(batch)
                if model and not r["label"] and not r["protect"] and r["kind"] not in ("sent", "drafts")
                and (r["ts"] or 0) < cutoff_ts]
        probs = dict(zip(need, model.junk_prob(ml.vectorize([ml.make_doc(batch[i]) for i in need])))) if need else {}
        rows = []
        for i, r in enumerate(batch):
            p = probs.get(i)
            if p is not None and not rules.resolve(r["user"], r["from_addr"], r["from_domain"], r["list_id"]):
                scored.append((p, r["size"] or 0))
            reason = decide(r, rules, cfg, cutoff_ts, p, threshold)
            if reason:
                rows.append((r["id"], reason))
                tally[reason.split(":", 1)[0]] += 1
            elif p is not None and p >= 0.5 and not rules.resolve(r["user"], r["from_addr"], r["from_domain"], r["list_id"]):
                borderline[r["org"]] += 1
        out.executemany("INSERT INTO plan(message, reason) VALUES (?, ?)", rows)
    conn.commit()
    steps = [(t, sum(1 for p, _ in scored if p >= t), sum(b for p, b in scored if p >= t)) for t in SCORE_STEPS]
    return {"tally": tally, "borderline": borderline, "missing_text": missing, "model": model is not None, "steps": steps,
            "model_file": model_file, "counts": training.counts()}


def gb(n):
    n = n or 0
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def report(conn, cutoff_ts, threshold, info, out_dir) -> str:
    os.makedirs(out_dir, exist_ok=True)
    cutoff = datetime.fromtimestamp(cutoff_ts, timezone.utc).date()
    n = info["counts"]
    lines = [f"# Cleanup plan, {date.today()}", "",
             f"Cutoff: **{cutoff}**. Model deletes at junk score ≥ **{threshold:.0%}**. "
             f"Trained on {n['junk'] + n['keep']:,} examples in training/ ({n['junk']:,} junk, {n['keep']:,} keep)"
             + (f", model `{os.path.basename(info['model_file'])}`" if info.get("model_file") else "")
             + ("" if info["model"] else ", **not enough labels to train: model not used**") + ".", ""]
    if info["missing_text"]:
        lines += [f"> {info['missing_text']:,} messages have no extracted text; run `maildir-cleanup extract`.", ""]

    lines += ["## Per user", "", "| user | messages | size | delete | delete size | keep size |", "|---|---:|---:|---:|---:|---:|"]
    for r in conn.execute("""SELECT m.user, count(*) n, sum(m.size) b, count(p.message) dn, sum(CASE WHEN p.message IS NOT NULL THEN m.size END) db
                             FROM messages m LEFT JOIN plan p ON p.message=m.id GROUP BY m.user ORDER BY b DESC"""):
        lines.append(f"| {r['user']} | {r['n']:,} | {gb(r['b'])} | {r['dn']:,} | {gb(r['db'])} | {gb((r['b'] or 0) - (r['db'] or 0))} |")
    t = conn.execute("SELECT count(*) n, sum(size) b FROM messages m JOIN plan p ON p.message=m.id").fetchone()
    a = conn.execute("SELECT count(*) n, sum(size) b FROM messages").fetchone()
    lines += ["", f"**Total: delete {t['n']:,} of {a['n']:,} messages, {gb(t['b'])} of {gb(a['b'])}.**", ""]

    lines += ["## By reason", "", "| reason | messages | size |", "|---|---:|---:|"]
    for r in conn.execute("""SELECT substr(p.reason, 1, instr(p.reason, ':') - 1) head, count(*) n, sum(m.size) b
                             FROM plan p JOIN messages m ON m.id=p.message GROUP BY head ORDER BY b DESC"""):
        lines.append(f"| {r['head']} | {r['n']:,} | {gb(r['b'])} |")

    if info["model"]:
        lines += ["", "## What other thresholds would delete", "",
                  "Old, unlabelled, unprotected mail by model junk score (set `junk_threshold` or `plan --threshold`).", "",
                  "| junk score ≥ | messages | size |", "|---:|---:|---:|"]
        for t, n, b in info["steps"]:
            mark = " ←" if abs(t - threshold) < 1e-9 else ""
            lines.append(f"| {t:.0%}{mark} | {n:,} | {gb(b)} |")

    lines += ["", "## Senders the model deletes most", "", "Spot-check these: `review` shows confident examples too.", "",
              "| sender domain | messages | size |", "|---|---:|---:|"]
    for r in conn.execute("""SELECT m.org, count(*) n, sum(m.size) b FROM plan p JOIN messages m ON m.id=p.message
                             WHERE p.reason LIKE 'model:%' GROUP BY m.org ORDER BY b DESC LIMIT 40"""):
        lines.append(f"| {r['org']} | {r['n']:,} | {gb(r['b'])} |")

    if info["borderline"]:
        lines += ["", f"## Leaning junk (50%+) but below {threshold:.0%}", "",
                  "More labels on these senders would settle them.", "", "| sender domain | messages |", "|---|---:|"]
        for org, n in info["borderline"].most_common(30):
            lines.append(f"| {org} | {n:,} |")

    lines += ["", "## Kept by protection signal (old mail)", "", "| signal | messages | size |", "|---|---:|---:|"]
    for r in conn.execute("""SELECT protect, count(*) n, sum(size) b FROM messages WHERE protect IS NOT NULL AND ts < ?
                             GROUP BY protect ORDER BY b DESC""", (cutoff_ts,)):
        lines.append(f"| {r['protect']} | {r['n']:,} | {gb(r['b'])} |")

    path = os.path.join(out_dir, f"plan-{date.today():%Y%m%d}.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def export(conn, out_dir) -> tuple[str, str, int]:
    """Write the deletion list as relpaths, plus a flag-tolerant keys list (dir<TAB>key)."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = f"{date.today():%Y%m%d}"
    paths = os.path.join(out_dir, f"delete-{stamp}.txt")
    keys = os.path.join(out_dir, f"delete-{stamp}.keys.txt")
    n = 0
    with open(paths, "w") as fp, open(keys, "w") as fk:
        for r in conn.execute("SELECT m.relpath, m.dir, m.key FROM plan p JOIN messages m ON m.id=p.message ORDER BY m.relpath"):
            fp.write(r["relpath"] + "\n")
            fk.write(f"{r['dir']}\t{r['key']}\n")
            n += 1
    return paths, keys, n
