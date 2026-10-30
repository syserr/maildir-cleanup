import argparse
import os
import sys
from datetime import datetime, timezone

from . import analyze as an
from . import cluster as cl
from . import config, db
from . import model as ml
from . import plan as pl
from .extract import extract
from .parse import split_name
from .rules import Rules
from .scan import scan
from .training import TrainingSet, import_legacy_labels


def gb(n):
    return f"{(n or 0) / 1e9:8.2f} GB"


def cmd_scan(cfg, conn, args):
    root = cfg.path(cfg.mail_root)
    if not root.is_dir():
        sys.exit(f"mail root {root} does not exist; pass --mail-root DIR or set mail_root in config.toml "
                 "(copy config.example.toml to start)")
    prev = db.get_meta(conn, "mail_root")
    if prev and prev != str(root):
        print(f"note: inventory was built from {prev}; now scanning {root}. "
              "Files not found under the new root are dropped from the inventory.", file=sys.stderr)
    db.set_meta(conn, "mail_root", root)
    res = scan(conn, str(root), args.workers or cfg.workers, args.user)
    print(f"scan: {res['added']:,} added, {res['removed']:,} removed, {res['total']:,} total")
    cmd_extract(cfg, conn, args)
    cmd_analyze(cfg, conn, args)


def cmd_extract(cfg, conn, args):
    n = extract(conn, str(cfg.path(cfg.mail_root)), getattr(args, "workers", 0) or cfg.workers)
    print(f"extract: body text for {n:,} messages")


def cmd_analyze(cfg, conn, args):
    counts = an.analyze(conn, cfg.protected_folders, cfg.keep_subject_patterns)
    prot = ", ".join(f"{k} {v:,}" for k, v in counts.most_common() if k)
    print(f"analyze: protected {sum(v for k, v in counts.items() if k):,} ({prot})")


def cmd_stats(cfg, conn, args):
    q = conn.execute
    t = q("SELECT count(*) n, sum(size) b, sum(error IS NOT NULL) e FROM messages").fetchone()
    print(f"{t['n']:,} messages, {gb(t['b']).strip()}, {t['e'] or 0:,} unparseable\n")
    print(f"{'user':<20}{'messages':>12}{'size':>12}")
    for r in q("SELECT user, count(*) n, sum(size) b FROM messages GROUP BY user ORDER BY b DESC"):
        print(f"{r['user']:<20}{r['n']:>12,}{gb(r['b'])}")
    print(f"\n{'folder kind':<20}{'messages':>12}{'size':>12}")
    for r in q("SELECT kind, count(*) n, sum(size) b FROM messages GROUP BY kind ORDER BY b DESC"):
        print(f"{r['kind']:<20}{r['n']:>12,}{gb(r['b'])}")
    print(f"\n{'year':<20}{'messages':>12}{'size':>12}")
    for r in q("SELECT strftime('%Y', ts, 'unixepoch') y, count(*) n, sum(size) b FROM messages GROUP BY y ORDER BY y"):
        print(f"{r['y'] or '?':<20}{r['n']:>12,}{gb(r['b'])}")
    print(f"\n{'top folders':<44}{'messages':>12}{'size':>12}")
    for r in q("SELECT user, folder, kind, count(*) n, sum(size) b FROM messages GROUP BY user, folder ORDER BY b DESC LIMIT ?", (args.top,)):
        name = f"{r['user']}:{r['folder']} ({r['kind']})"
        print(f"{name[:43]:<44}{r['n']:>12,}{gb(r['b'])}")


def cmd_clusters(cfg, conn, args):
    rules = Rules(cfg.path(cfg.rules))
    cutoff = cfg.cutoff_ts(args.cutoff)
    clusters = cl.ranked(cl.build(conn, cutoff, rules, unresolved_only=not args.all, users=args.user))
    print(f"{len(clusters):,} clusters{'' if args.all else ' not yet covered by rules'}; top {args.top} by deletable size before cutoff\n")
    print(f"{'cluster':<44}{'msgs':>8}{'old MB':>9}{'bulk':>6}{'read':>6}{'prot':>6}")
    for c in clusters[: args.top]:
        n = max(c.n, 1)
        print(f"{c.key[:43]:<44}{c.n:>8,}{c.deletable_bytes / 1e6:>9.1f}{c.bulk / n:>6.0%}{c.seen / n:>6.0%}"
              f"{sum(c.protected.values()):>6}")
        if args.subjects:
            for ts, subj in c.sample_subjects(args.subjects):
                print(f"    {datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%d}  {subj[:90]}")


def parse_threshold(s: str) -> float:
    """Junk score as 0.7, 70 or 70%."""
    try:
        v = float(s.rstrip("%"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a percentage: {s!r}")
    if s.endswith("%") or v > 1:
        v /= 100
    if not 0 < v <= 1:
        raise argparse.ArgumentTypeError(f"threshold must be between 0 and 100%: {s!r}")
    return v


def threshold(cfg, args) -> float:
    return cfg.junk_threshold if args.threshold is None else args.threshold


def cmd_review(cfg, conn, args):
    from . import tui

    done = tui.run(conn, cfg.cutoff_ts(args.cutoff), str(cfg.path(cfg.mail_root)), args.training, threshold(cfg, args),
                   args.user, args.grep)
    print(f"review: {done} messages labelled this session; training set now {sum(args.training.counts().values()):,} examples")


def models_dir(cfg):
    return cfg.path("data/models")


def cmd_train(cfg, conn, args):
    from .pool import Pool

    training = args.training
    rep = ml.evaluate(conn, training)
    for k in ("examples", "junk", "keep", "error", "cv_accuracy", "junk_called_keep", "keep_called_junk", "live_agreement"):
        if k in rep:
            v = rep[k]
            print(f"{k}: {v:.1%}" if isinstance(v, float) else f"{k}: {v}")
    for title, key in (("most junk-ish", "junk_tokens"), ("most keep-ish", "keep_tokens")):
        if key in rep:
            print(f"\n{title} (@ = sender/header signal, s: = subject word):")
            print("  " + ", ".join(f"{t} {w:+.2f}" for w, t in rep[key]))
    model = ml.train(conn, training)
    if not model:
        return
    previous = ml.latest_version(models_dir(cfg))
    path, saved = ml.save_version(model, models_dir(cfg), training, {"cv_accuracy": rep.get("cv_accuracy")})
    if not saved:
        print(f"\nmodel: training set unchanged since {os.path.relpath(path)}; nothing new saved")
        return
    print(f"\nmodel: saved {os.path.relpath(path)}")
    if previous:
        old_meta, thr = previous[1], threshold(cfg, args)
        acc = lambda m: f"{m['cv_accuracy']:.1%}" if m.get("cv_accuracy") is not None else "?"
        print(f"  vs {previous[0].name}: examples {old_meta['examples']:,} → {rep['examples']:,}, "
              f"cv accuracy {acc(old_meta)} → {acc(rep)}")
        training.to_db(conn)
        pool = Pool(conn, cfg.cutoff_ts(args.cutoff), size=5000, seed=0)
        if pool.X is not None:
            before, after = ml.load(previous[0]).junk_prob(pool.X) >= thr, model.junk_prob(pool.X) >= thr
            scale = pool.eligible_n / max(len(pool.rows), 1)
            print(f"  on a sample of {len(pool.rows):,} unlabelled old messages at ≥{thr:.0%}: "
                  f"{int((after & ~before).sum())} newly junk, {int((before & ~after).sum())} no longer junk; "
                  f"estimated deletions ≈{int(before.sum() * scale):,} → ≈{int(after.sum() * scale):,}")


def cmd_plan(cfg, conn, args):
    rules = Rules(cfg.path(cfg.rules))
    cutoff = cfg.cutoff_ts(args.cutoff)
    info = pl.build_plan(conn, args.training, rules, cfg, cutoff, threshold(cfg, args), models_dir(cfg))
    path = pl.report(conn, cutoff, threshold(cfg, args), info, str(cfg.path("reports")))
    if not info["model"]:
        print("plan: not enough labels to train the model; only labels, rules and folder cleanup apply")
    if info["missing_text"]:
        print(f"plan: {info['missing_text']:,} messages have no body text yet; run `maildir-cleanup extract`")
    print("plan: delete " + ", ".join(f"{k} {v:,}" for k, v in info["tally"].most_common()))
    print(f"report: {os.path.relpath(path)}")


def find_message(conn, root, path):
    """Inventory row for a message file given as a path (absolute or relative to the mail root) or a bare filename.
    Falls back to the filename key, so a file whose flags changed since the scan is still found."""
    rel = os.path.relpath(os.path.abspath(path), root) if os.path.isabs(path) or os.path.exists(path) else path
    q = """SELECT m.*, coalesce(t.text, '') AS text, l.label FROM messages m
           LEFT JOIN texts t ON t.message = m.id LEFT JOIN temp.labelled l ON l.dir = m.dir AND l.key = m.key WHERE """
    if r := conn.execute(q + "m.relpath = ?", (rel,)).fetchone():
        return r
    key = split_name(os.path.basename(rel))[0]
    rows = conn.execute(q + "m.key = ?", (key,)).fetchall()
    return rows[0] if len(rows) == 1 else None


def cmd_score(cfg, conn, args):
    root = str(cfg.path(cfg.mail_root))
    cutoff, thr = cfg.cutoff_ts(args.cutoff), threshold(cfg, args)
    args.training.to_db(conn)
    model = ml.train(conn, args.training)
    if not model:
        print("score: no model yet (label some junk and some keep in `review` first); showing plan decisions only\n")
    rules = Rules(cfg.path(cfg.rules))
    for path in args.files:
        r = find_message(conn, root, path)
        if r is None:
            print(f"{path}\n  not in the inventory (run `scan` if it is new)\n")
            continue
        doc = ml.make_doc(r)
        p = float(model.junk_prob(ml.vectorize([doc]))[0]) if model else None
        when = datetime.fromtimestamp(r["ts"] or 0, timezone.utc)
        print(r["relpath"])
        print(f"  from:    {r['from_name'] or ''} <{r['from_addr'] or ''}>")
        print(f"  subject: {' '.join((r['subject'] or '').split())}")
        print(f"  date:    {when:%Y-%m-%d} ({'old' if (r['ts'] or 0) < cutoff else 'recent'}; cutoff {datetime.fromtimestamp(cutoff, timezone.utc):%Y-%m-%d})"
              f"   folder: {r['user']} / {r['folder']}")
        if p is not None:
            print(f"  score:   junk {p:.0%}  (deletes at ≥{thr:.0%})")
            junk, keep = model.explain(doc)
            print("  why:     junk ← " + (", ".join(f"{t} {w:+.1f}" for t, w in junk) or "—"))
            print("           keep ← " + (", ".join(f"{t} {w:+.1f}" for t, w in keep[:6]) or "—"))
        if r["label"]:
            print(f"  label:   {r['label']} (in training/; your label decides this message, not the score)")
        if r["protect"]:
            print(f"  protect: {r['protect']}")
        reason = pl.decide(r, rules, cfg, cutoff, p, thr)
        print(f"  plan:    {'DELETE (' + reason + ')' if reason else 'keep'}\n")


def cmd_export(cfg, conn, args):
    paths, keys, n = pl.export(conn, str(cfg.path("out")))
    print(f"export: {n:,} files\n  {os.path.relpath(paths)}  (paths relative to the mail root)\n"
          f"  {os.path.relpath(keys)}  (folder<TAB>key; use with scripts/apply-list.sh if flags may have changed)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="maildir-cleanup", description="List old bulk mail and spam in Maildirs for deletion.")
    p.add_argument("--config", default="config.toml", help="settings file (default: config.toml)")
    p.add_argument("--mail-root", "-m", metavar="DIR", help="mail directory, one subdirectory per user (overrides mail_root in config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("scan", cmd_scan, "inventory message headers, extract body text, analyze (all resumable)")
    sp.add_argument("--user", action="append", help="only scan this user (repeatable)")
    sp.add_argument("--workers", type=int, default=0)
    sp = add("extract", cmd_extract, "store body text of messages that have none yet (resumable)")
    sp.add_argument("--workers", type=int, default=0)
    add("analyze", cmd_analyze, "recompute protection signals (replies, flags, correspondents, record subjects)")
    sp = add("stats", cmd_stats, "sizes by user, folder kind, year")
    sp.add_argument("--top", type=int, default=25)
    sp = add("clusters", cmd_clusters, "list sender clusters by reclaimable size")
    sp.add_argument("--top", type=int, default=40)
    sp.add_argument("--subjects", type=int, default=0, help="show N sample subjects per cluster")
    sp.add_argument("--all", action="store_true", help="include clusters already covered by rules")
    sp.add_argument("--user", action="append")
    sp.add_argument("--cutoff", help="YYYY-MM-DD (default: today minus cutoff_days)")
    sp = add("review", cmd_review, "label messages junk/keep in a terminal UI; the model retrains as you go")
    sp.add_argument("--user", help="only show messages from this mailbox")
    sp.add_argument("--grep", "-g", action="append", metavar="WORD",
                    help="only show messages with WORD in the subject, sender, recipients, list or body "
                         "(case-insensitive; repeat to require several words)")
    sp.add_argument("--threshold", "-t", type=parse_threshold, help="junk score the estimate assumes, e.g. 70 (default: junk_threshold in config)")
    sp.add_argument("--cutoff")
    sp = add("score", cmd_score, "show the junk score of message files, why, and what plan would do with them")
    sp.add_argument("files", nargs="+", metavar="FILE", help="message file: full path, path relative to the mail root, or just the filename")
    sp.add_argument("--threshold", "-t", type=parse_threshold, help="default: junk_threshold in config")
    sp.add_argument("--cutoff")
    sp = add("train", cmd_train, "check the model against training/, save a new model version, compare with the last")
    sp.add_argument("--threshold", "-t", type=parse_threshold, help="for the comparison (default: junk_threshold in config)")
    sp.add_argument("--cutoff")
    sp = add("plan", cmd_plan, "decide deletions and write reports/plan-YYYYMMDD.md")
    sp.add_argument("--cutoff")
    sp.add_argument("--threshold", "-t", type=parse_threshold, help="delete when the junk score is at least this, e.g. 70 (default: junk_threshold in config)")
    add("export", cmd_export, "write out/delete-YYYYMMDD.txt from the last plan")

    args = p.parse_args(argv)
    cfg = config.load(args.config)
    if args.mail_root:
        cfg.mail_root = os.path.abspath(args.mail_root)
    conn = db.connect(str(cfg.path(cfg.db)))
    args.training = TrainingSet(cfg.path(cfg.training))
    n, problems = import_legacy_labels(conn, args.training, str(cfg.path(cfg.mail_root)))
    if n:
        print(f"moved {n:,} labels from {cfg.db} into {cfg.training}/", file=sys.stderr)
    for p in problems:
        print(f"warning: label not moved, kept in {cfg.db}: {p}", file=sys.stderr)
    args.fn(cfg, conn, args)
