import os

from maildir_cleanup.parse import org_domain, split_name
from maildir_cleanup.scan import scan

from .conftest import row


def test_counts_and_layout(env):
    conn, rel = env["conn"], env["rel"]
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == len(rel)
    folders = {(r[0], r[1], r[2]) for r in conn.execute("SELECT DISTINCT user, folder, kind FROM messages")}
    assert ("alice", "INBOX", "inbox") in folders
    assert ("alice", "Sent", "sent") in folders
    assert ("alice", "Junk", "junk") in folders
    assert ("alice", "Archive.2015", "other") in folders
    assert ("bob", "INBOX", "inbox") in folders
    assert ("bob", "Lists/Linux", "other") in folders
    assert ("example.org/carol", "INBOX", "inbox") in folders
    assert ("example.org/carol", "MigratedMail.Inbox", "inbox") in folders


def test_header_fields(env):
    conn, rel = env["conn"], env["rel"]
    r = row(conn, rel["ad_old"])
    assert r["from_addr"] == "news@shop.example" and r["org"] == "shop.example"
    assert r["list_unsub"] == 1 and r["return_domain"] == "mail.shop.example"
    assert r["flags"] == "S" and r["key"] + ":2,S" == r["name"]
    assert row(conn, rel["ad_new"])["sub"] == "new"
    assert row(conn, rel["spamhdr"])["spam_flag"] == 1
    assert row(conn, rel["spamhdr"])["spam_score"] == 9.1
    assert row(conn, rel["bob_list"])["list_id"] == "linux.lists.example"


def test_compressed(env):
    for k in ("ad_gz", "ad_zst"):
        r = row(env["conn"], env["rel"][k])
        assert r["error"] is None and r["from_addr"] == "news@shop.example"


def test_rescan_is_incremental_and_tracks_renames(env):
    conn, root, rel = env["conn"], env["root"], env["rel"]
    assert scan(conn, root, workers=1, progress=False)["added"] == 0
    old = os.path.join(root, rel["ad_recent"])
    os.rename(old, old + "S")   # flags "" -> "S", as an IMAP client reading it would
    res = scan(conn, root, workers=1, progress=False)
    assert res["added"] == 1 and res["removed"] == 1
    assert row(conn, rel["ad_recent"] + "S")["flags"] == "S"


def test_helpers():
    assert split_name("123.M1P2.h,S=5:2,RS") == ("123.M1P2.h,S=5", "RS")
    assert split_name("123.M1P2.h") == ("123.M1P2.h", "")
    assert org_domain("mail.news.shop.co.uk") == "shop.co.uk"
    assert org_domain("e.store.com") == "store.com"
