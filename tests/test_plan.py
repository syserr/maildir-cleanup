import subprocess
import os
from pathlib import Path

from maildir_cleanup import cluster as cl
from maildir_cleanup import model as ml
from maildir_cleanup import plan as pl
from maildir_cleanup.rules import Rule, Rules

from . import fixtures as fx
from .conftest import label, rescan, row

APPLY = Path(__file__).resolve().parent.parent / "scripts" / "apply-list.sh"


def planned(env, rules=None, threshold=0.9):
    cfg, conn = env["cfg"], env["conn"]
    pl.build_plan(conn, env["training"], rules or Rules(env["tmp"] / "rules.toml"), cfg, cfg.cutoff_ts(), threshold)
    ids = {r[0]: r[1] for r in conn.execute("SELECT m.relpath, p.reason FROM plan p JOIN messages m ON m.id=p.message")}
    inv = {v: k for k, v in env["rel"].items()}
    return {inv.get(k, k): v for k, v in ids.items()}


def test_protection_signals(env):
    conn, rel = env["conn"], env["rel"]
    assert row(conn, rel["friend"])["protect"] == "correspondent"
    assert row(conn, rel["support"])["protect"] == "thread"
    assert row(conn, rel["ad_flagged"])["protect"] == "flagged"
    assert row(conn, rel["archive_ad"])["protect"] == "folder"
    assert row(conn, rel["sent_friend"])["protect"] == "sent"
    assert row(conn, rel["stranger"])["protect"] is None
    assert row(conn, rel["receipt"])["protect"] == "keep-subject"
    assert row(conn, rel["junk_invoice"])["protect"] is None   # phishing-style subjects in junk are not trusted


def test_no_rules_only_folder_and_spam_cleanup(env):
    got = planned(env, Rules(env["tmp"] / "rules.toml"))
    assert set(got) == {"junk_recent", "junk_invoice", "trash_old", "spamhdr"}


def test_ad_rule_respects_cutoff_and_protection(env):
    rules = Rules(env["tmp"] / "rules.toml")
    rules.add(Rule("domain", "shop.example", "ad"))
    got = planned(env, rules)
    assert {"ad_old", "ad_old2", "ad_new", "ad_gz", "ad_zst", "bob_ad"} <= set(got)
    for keep in ("receipt", "ad_recent", "ad_flagged", "archive_ad", "support", "friend", "sent_friend", "stranger", "trash_recent", "bob_list"):
        assert keep not in got, keep


def test_rule_precedence_and_user_scope(env):
    rules = Rules(env["tmp"] / "rules.toml")
    rules.add(Rule("domain", "shop.example", "ad"))
    rules.add(Rule("domain", "shop.example", "keep", user="bob"))
    rules.add(Rule("address", "x@spammy.example", "protect"))
    got = planned(env, rules)
    assert "bob_ad" not in got and "ad_old" in got and "spamhdr" not in got


def test_rules_roundtrip(env):
    path = env["tmp"] / "rules.toml"
    rules = Rules(path)
    rules.add(Rule("domain", "Shop.Example", "ad", note="store ads"))
    rules.add(Rule("list_id", "linux.lists.example", "keep", user="bob"))
    rules.save()
    again = Rules(path)
    assert again.resolve("alice", "news@shop.example", "mail.shop.example", "").action == "ad"
    assert again.resolve("bob", "dev@lists.example", "lists.example", "linux.lists.example").action == "keep"
    assert again.resolve("alice", "dev@lists.example", "lists.example", "linux.lists.example") is None


def test_clusters(env):
    clusters = cl.build(env["conn"], env["cfg"].cutoff_ts())
    shop = clusters["domain:shop.example"]
    assert shop.n == 11 and len(shop.users) == 2 and shop.protected["flagged"] == 1
    assert "address:friend@gmail.com" in clusters and "address:someone@gmail.com" in clusters


def test_export_and_apply_after_flag_change(env, tmp_path):
    rules = Rules(env["tmp"] / "rules.toml")
    rules.add(Rule("domain", "shop.example", "ad"))
    planned(env, rules)
    paths, keys, n = pl.export(env["conn"], str(env["tmp"] / "out"))
    lines = Path(paths).read_text().splitlines()
    assert len(lines) == n and all(os.path.exists(os.path.join(env["root"], p)) for p in lines)

    # The live server has moved one message new -> cur and marked another read since the scan.
    root = env["root"]
    new = os.path.join(root, env["rel"]["ad_new"])
    os.rename(new, new.replace("/new/", "/cur/") + ":2,S")
    old = os.path.join(root, env["rel"]["ad_old"])
    os.rename(old, old + "R")

    dry = subprocess.run(["bash", APPLY, root, keys], capture_output=True, text=True, check=True).stdout
    assert f"{n} files" in dry and "0 not found" in dry
    plain = subprocess.run(["bash", APPLY, root, paths], capture_output=True, text=True, check=True).stdout
    assert "2 not found" in plain

    q = tmp_path / "quarantine"
    subprocess.run(["bash", APPLY, root, keys, "--move", str(q)], capture_output=True, text=True, check=True)
    assert len([f for f in q.rglob("*") if f.is_file()]) == n
    assert not os.path.exists(old + "R")


def test_human_labels_decide_their_own_message(env):
    label(env, env["rel"]["stranger"], "junk")
    label(env, env["rel"]["ad_recent"], "junk")     # recent: junk only means "delete once old"
    label(env, env["rel"]["spamhdr"], "keep")       # keep overrides the server's spam flag
    label(env, env["rel"]["ad_flagged"], "junk")    # protection still wins over a label
    got = planned(env)
    assert got["stranger"] == "label: junk"
    for k in ("ad_recent", "spamhdr", "ad_flagged"):
        assert k not in got, k


def test_model_deletes_unlabelled_lookalikes(env):
    ads, notes = fx.corpus(env["root"])
    rescan(env)
    for rel in ads[:5]:
        label(env, rel, "junk")
    for rel in notes[:5]:
        label(env, rel, "keep")
    got = planned(env, threshold=0.6)
    assert all(rel in got for rel in ads), [rel for rel in ads if rel not in got]
    assert not any(rel in got for rel in notes)
    assert all(got[rel].startswith("model:") for rel in ads[5:])

    rep = ml.evaluate(env["conn"], env["training"])
    assert rep["examples"] == 10 and rep["cv_accuracy"] >= 0.8
    assert any(t.startswith("@") for _, t in rep["junk_tokens"])


def test_no_model_without_both_kinds_of_label(env):
    ads, _ = fx.corpus(env["root"])
    rescan(env)
    for rel in ads[:5]:
        label(env, rel, "junk")
    assert ml.train(env["conn"], env["training"]) is None
    got = planned(env)
    assert set(ads[:5]) <= set(got) and not set(ads[5:]) & set(got)


def test_explain_uses_the_same_hashing_as_the_vectorizer():
    doc = (["@org:store.example", "@unsubscribe"], "Huge sale", "coupon inside")
    X = ml.vectorize([doc])
    for tok in ("@org:store.example", "s:huge", "s:huge_sale", "coupon"):
        assert X[0, ml._index(tok)] > 0, tok


def test_threshold_decides_on_junk_score(env):
    cfg, conn = env["cfg"], env["conn"]
    r = dict(row(conn, env["rel"]["stranger"]), label=None)
    rules = Rules(env["tmp"] / "rules.toml")
    assert pl.decide(r, rules, cfg, cfg.cutoff_ts(), 0.75, 0.7) == "model: 0.75"
    assert pl.decide(r, rules, cfg, cfg.cutoff_ts(), 0.65, 0.7) is None
    assert pl.decide(dict(r, label="keep"), rules, cfg, cfg.cutoff_ts(), 0.99, 0.7) is None


def test_threshold_parsing_and_config_default(env, tmp_path):
    import argparse
    import pytest
    from maildir_cleanup import cli, config

    assert cli.parse_threshold("70") == cli.parse_threshold("70%") == cli.parse_threshold("0.7") == 0.7
    assert cli.parse_threshold("1") == 1.0
    for bad in ("0", "150", "-5", "lots"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.parse_threshold(bad)
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("junk_threshold = 0.7\n")
    cfg = config.load(str(cfg_file))
    assert cli.threshold(cfg, argparse.Namespace(threshold=None)) == 0.7
    assert cli.threshold(cfg, argparse.Namespace(threshold=0.8)) == 0.8
    cfg_file.write_text("junk_threshold = 70\n")
    with pytest.raises(SystemExit):
        config.load(str(cfg_file))


def test_report_shows_threshold_table(env):
    ads, notes = fx.corpus(env["root"])
    rescan(env)
    for rel in ads[:5]:
        label(env, rel, "junk")
    for rel in notes[:5]:
        label(env, rel, "keep")
    cfg, conn = env["cfg"], env["conn"]
    info = pl.build_plan(conn, env["training"], Rules(env["tmp"] / "rules.toml"), cfg, cfg.cutoff_ts(), 0.7)
    counts = [n for _, n, _ in info["steps"]]
    assert counts == sorted(counts, reverse=True) and counts[0] >= len(ads) - 5
    text = Path(pl.report(conn, cfg.cutoff_ts(), 0.7, info, str(env["tmp"] / "reports"))).read_text()
    assert "junk score ≥ **70%**" in text and "| 70% ← |" in text


def test_score_command_shows_score_and_decision(env, capsys):
    import argparse
    from maildir_cleanup import cli

    ads, notes = fx.corpus(env["root"])
    rescan(env)
    for rel in ads[:5]:
        label(env, rel, "junk")
    for rel in notes[:5]:
        label(env, rel, "keep")
    cfg = env["cfg"]
    cfg.mail_root = env["root"]
    renamed = os.path.join(env["root"], ads[8]) + "S"           # flags changed since the scan: found by key
    os.rename(os.path.join(env["root"], ads[8]), renamed)
    files = [renamed, ads[9], os.path.basename(notes[9]), ads[0], env["rel"]["receipt"], "nope:2,S"]
    cli.cmd_score(cfg, env["conn"], argparse.Namespace(files=files, threshold=0.6, cutoff=None, training=env["training"]))
    out = capsys.readouterr().out
    blocks = out.strip().split("\n\n")
    assert len(blocks) == 6
    assert "junk" in blocks[0] and "DELETE (model:" in blocks[0] and "why:" in blocks[0]
    assert "DELETE (model:" in blocks[1]
    assert "plan:    keep" in blocks[2]
    assert "label:   junk" in blocks[3] and "DELETE (label: junk)" in blocks[3]
    assert "protect: keep-subject" in blocks[4] and "plan:    keep" in blocks[4]
    assert "not in the inventory" in blocks[5]
