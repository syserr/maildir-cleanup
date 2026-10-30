import os

import pytest

from maildir_cleanup import config, db
from maildir_cleanup.analyze import analyze
from maildir_cleanup.extract import extract
from maildir_cleanup.scan import scan
from maildir_cleanup.training import TrainingSet

from . import fixtures


@pytest.fixture
def env(tmp_path):
    """A project dir with a synthetic mail clone, scanned and analyzed."""
    root = tmp_path / "mail"
    paths = fixtures.build(str(root))
    (tmp_path / "config.toml").write_text('protected_folders = ["Archive.2015"]\n')
    cfg = config.load(str(tmp_path / "config.toml"))
    conn = db.connect(str(cfg.path(cfg.db)))
    scan(conn, str(root), workers=2, progress=False)
    extract(conn, str(root), workers=2, progress=False)
    analyze(conn, cfg.protected_folders, cfg.keep_subject_patterns)
    rel = {k: os.path.relpath(v, root) for k, v in paths.items()}
    training = TrainingSet(cfg.path(cfg.training))
    return {"cfg": cfg, "conn": conn, "root": str(root), "rel": rel, "tmp": tmp_path, "training": training}


def row(conn, relpath):
    return conn.execute("SELECT * FROM messages WHERE relpath=?", (relpath,)).fetchone()


def rescan(env):
    """Pick up messages added to the tree after the fixture was built."""
    scan(env["conn"], env["root"], workers=1, progress=False)
    extract(env["conn"], env["root"], workers=1, progress=False)
    analyze(env["conn"], env["cfg"].protected_folders, env["cfg"].keep_subject_patterns)


def label(env, rel, value):
    r = row(env["conn"], rel)
    return env["training"].add(os.path.join(env["root"], r["relpath"]), value, dict(r))
