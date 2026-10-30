import os

from maildir_cleanup import model as ml
from maildir_cleanup.training import TrainingSet, import_legacy_labels

from . import fixtures as fx
from .conftest import label, rescan, row
from .test_plan import planned


def test_label_copies_message_into_training(env):
    rel = env["rel"]["ad_gz"]
    name = label(env, rel, "junk")
    tr = env["training"]
    label_, path = tr.files[name]
    assert label_ == "junk" and path.parent == tr.path / "junk" / "cur"
    assert path.read_bytes().startswith(b"From:")              # stored uncompressed
    r = row(env["conn"], rel)
    assert tr.label_of(r["dir"], r["key"]) == "junk"
    again = TrainingSet(tr.path)                                 # everything is on disk
    assert again.label_of(r["dir"], r["key"]) == "junk" and again.by_origin[(r["dir"], r["key"])]["kind"] == "inbox"


def test_training_survives_deleting_the_labelled_mail(env):
    ads, notes = fx.corpus(env["root"])
    rescan(env)
    for rel in ads[:5]:
        label(env, rel, "junk")
    for rel in notes[:5]:
        label(env, rel, "keep")
    before = ml.labelled(env["conn"], env["training"])[0]
    for rel in ads[:5] + notes[:5]:                              # the cleanup deletes them on the server...
        os.unlink(os.path.join(env["root"], rel))
    rescan(env)                                                  # ...and the next pull + scan drops them
    assert env["conn"].execute("SELECT count(*) FROM messages WHERE user='dave'").fetchone()[0] == 14
    env["conn"].execute("DELETE FROM example_docs")              # also rebuildable from the copies alone
    docs, y = ml.labelled(env["conn"], env["training"])
    assert len(docs) == 10 and y.sum() == 5
    assert [list(d) for d in before] == [list(d) for d in docs]  # same features as read from the inventory
    got = planned(env, threshold=0.6)
    assert all(got.get(rel, "").startswith("model:") for rel in ads[5:])
    assert not any(rel in got for rel in notes[5:])


def test_inventory_and_copy_give_the_same_document(env):
    rel = env["rel"]["ad_old"]
    name = label(env, rel, "junk")
    r = env["conn"].execute(f"""SELECT {ml.DOC_COLUMNS} FROM messages m LEFT JOIN texts t ON t.message = m.id
                                WHERE m.relpath = ?""", (rel,)).fetchone()
    _, path = env["training"].files[name]
    assert list(ml.make_doc(r)) == list(ml.example_doc(path, r["kind"]))


def test_relabel_by_moving_the_file_and_undo(env):
    tr = env["training"]
    a, b = env["rel"]["ad_old"], env["rel"]["stranger"]
    na, nb = label(env, a, "junk"), label(env, b, "keep")
    _, path = tr.files[na]
    keep_dir = tr.path / "keep" / "cur"
    os.rename(path, keep_dir / path.name)                       # what a mail client's "move" does
    tr = TrainingSet(tr.path)
    ra = row(env["conn"], a)
    assert tr.label_of(ra["dir"], ra["key"]) == "keep"
    rb = row(env["conn"], b)
    tr.remove(rb["dir"], rb["key"])
    assert nb not in tr.files and tr.label_of(rb["dir"], rb["key"]) is None
    assert nb not in TrainingSet(tr.path).files


def test_same_message_in_two_mailboxes_shares_one_copy(env):
    msg = fx.message("Shop <news@shop.example>", "Shared sale", fx.OLD, extra=fx.AD, msgid="<shared-1@shop.example>")
    pa = fx.deliver(os.path.join(env["root"], "alice"), msg, fx.OLD)
    pb = fx.deliver(os.path.join(env["root"], "bob", "Maildir"), msg, fx.OLD)
    rescan(env)
    ra, rb = (os.path.relpath(p, env["root"]) for p in (pa, pb))
    assert label(env, ra, "junk") == label(env, rb, "junk")
    tr = env["training"]
    assert len(tr.files) == 1 and len(tr.rows) == 2
    got = planned(env)
    assert got[ra] == got[rb] == "label: junk"


def test_legacy_labels_move_into_training(env):
    conn = env["conn"]
    conn.execute("CREATE TABLE labels (dir TEXT, key TEXT, label TEXT, guess REAL, at TEXT, PRIMARY KEY (dir, key))")
    for k, lab in (("ad_old", "junk"), ("stranger", "keep")):
        r = row(conn, env["rel"][k])
        conn.execute("INSERT INTO labels VALUES (?, ?, ?, 0.5, '2026-10-30 12:00:00')", (r["dir"], r["key"], lab))
    n, problems = import_legacy_labels(conn, env["training"], env["root"])
    assert (n, problems) == (2, [])
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='labels'").fetchone()
    tr = TrainingSet(env["training"].path)
    assert tr.counts() == {"junk": 1, "keep": 1} and len(tr.guesses()) == 2


def test_legacy_labels_kept_when_mail_is_missing(env):
    conn = env["conn"]
    conn.execute("CREATE TABLE labels (dir TEXT, key TEXT, label TEXT, guess REAL, at TEXT, PRIMARY KEY (dir, key))")
    r = row(conn, env["rel"]["ad_old"])
    conn.execute("INSERT INTO labels VALUES (?, ?, 'junk', NULL, NULL)", (r["dir"], r["key"]))
    conn.execute("INSERT INTO labels VALUES ('gone', 'nothing', 'junk', NULL, NULL)")
    n, problems = import_legacy_labels(conn, env["training"], env["root"])
    assert n == 1 and len(problems) == 1
    assert conn.execute("SELECT count(*) FROM labels").fetchone()[0] == 2     # not dropped


def test_model_versions_only_when_training_changes(env, tmp_path):
    ads, notes = fx.corpus(env["root"])
    rescan(env)
    for rel in ads[:3]:
        label(env, rel, "junk")
    for rel in notes[:3]:
        label(env, rel, "keep")
    tr, models = env["training"], tmp_path / "models"
    m = ml.train(env["conn"], tr)
    first, saved = ml.save_version(m, models, tr)
    assert saved and first.exists() and first.with_suffix(".json").exists()
    assert ml.save_version(m, models, tr) == (first, False)
    label(env, ads[3], "junk")
    second, saved = ml.save_version(ml.train(env["conn"], tr), models, tr)
    assert saved and second != first and ml.latest_version(models)[0] == second
    assert ml.latest_version(models)[1]["examples"] == 7
    assert ml.load(second).junk_prob(ml.vectorize([ml.example_doc(tr.files[label(env, ads[4], "junk")][1], "inbox")]))[0] > 0.5
