"""Synthetic Maildir trees for tests. No real mail is used."""

import gzip
import os
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import zstandard

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=365 * 6)
RECENT = NOW - timedelta(days=30)

_seq = 0


def message(frm, subject, when, to="alice@home.example", msgid=None, extra=None, body="hello\n"):
    global _seq
    _seq += 1
    headers = {
        "From": frm,
        "To": to,
        "Subject": subject,
        "Date": format_datetime(when),
        "Message-ID": msgid or f"<m{_seq}@test>",
    }
    headers.update(extra or {})
    return ("".join(f"{k}: {v}\n" for k, v in headers.items()) + "\n" + body).encode()


def deliver(folder_dir, data, when, flags="S", sub="cur", compress=None):
    """Write a message the way Dovecot names it; returns its path."""
    global _seq
    _seq += 1
    for d in ("cur", "new", "tmp"):
        os.makedirs(os.path.join(folder_dir, d), exist_ok=True)
    name = f"{int(when.timestamp())}.M{_seq}P1.testhost,S={len(data)}"
    if sub == "cur":
        name += f":2,{flags}"
    if compress == "gz":
        data = gzip.compress(data)
    elif compress == "zst":
        data = zstandard.ZstdCompressor().compress(data)
    path = os.path.join(folder_dir, sub, name)
    with open(path, "wb") as f:
        f.write(data)
    os.utime(path, (time.time(), when.timestamp()))
    return path


AD = {"List-Unsubscribe": "<mailto:unsub@shop.example>", "Return-Path": "<bounce-123@mail.shop.example>"}


def build(root):
    """alice: Maildir++ at the user dir. bob: nested Maildir/ with an fs-layout subfolder.

    Returns {label: path} for messages tests care about."""
    p = {}
    a = os.path.join(root, "alice")
    p["ad_old"] = deliver(a, message("Shop <news@shop.example>", "50% off sale 2019", OLD, extra=AD), OLD)
    p["ad_old2"] = deliver(a, message("Shop <news@shop.example>", "60% off sale 2018", OLD - timedelta(days=40), extra=AD), OLD - timedelta(days=40))
    p["ad_recent"] = deliver(a, message("Shop <news@shop.example>", "70% off sale", RECENT, extra=AD), RECENT, flags="")
    p["ad_flagged"] = deliver(a, message("Shop <news@shop.example>", "Your coupon", OLD, extra=AD), OLD, flags="FS")
    p["ad_new"] = deliver(a, message("Shop <news@shop.example>", "Unread sale", OLD, extra=AD), OLD, sub="new")
    p["ad_gz"] = deliver(a, message("Shop <news@shop.example>", "Gz sale", OLD, extra=AD), OLD, compress="gz")
    p["ad_zst"] = deliver(a, message("Shop <news@shop.example>", "Zst sale", OLD, extra=AD), OLD, compress="zst")
    p["support"] = deliver(a, message("Shop Support <help@shop.example>", "Your order problem", OLD, msgid="<order-1@shop.example>"), OLD)
    p["friend"] = deliver(a, message("Friend <friend@gmail.com>", "Dinner?", OLD), OLD)
    p["stranger"] = deliver(a, message("Someone <someone@gmail.com>", "hi", OLD), OLD)
    p["receipt"] = deliver(a, message("Shop <news@shop.example>", "Your receipt for order 1234", OLD, extra=AD), OLD)
    p["junk_invoice"] = deliver(os.path.join(a, ".Junk"), message("x@junk.example", "Invoice overdue", OLD), OLD)
    p["spamhdr"] = deliver(a, message("x@spammy.example", "WIN", OLD, extra={"X-Spam-Status": "Yes, score=9.1 required=5.0"}), OLD)

    sent = os.path.join(a, ".Sent")
    p["sent_friend"] = deliver(sent, message("Alice <alice@home.example>", "Re: Dinner?", OLD, to="Friend <friend@gmail.com>"), OLD)
    p["sent_support"] = deliver(sent, message("Alice <alice@home.example>", "Re: Your order problem", OLD, to="help@shop.example",
                                              extra={"In-Reply-To": "<order-1@shop.example>"}), OLD)
    p["junk_recent"] = deliver(os.path.join(a, ".Junk"), message("x@junk.example", "pills", RECENT), RECENT)
    p["trash_old"] = deliver(os.path.join(a, ".Trash"), message("x@other.example", "old thing", OLD), OLD)
    p["trash_recent"] = deliver(os.path.join(a, ".Trash"), message("x@other.example", "new thing", RECENT), RECENT)
    p["archive_ad"] = deliver(os.path.join(a, ".Archive.2015"), message("Shop <news@shop.example>", "kept sale", OLD, extra=AD), OLD)

    b = os.path.join(root, "bob", "Maildir")
    p["bob_ad"] = deliver(b, message("Shop <news@shop.example>", "Bob sale", OLD, to="bob@home.example", extra=AD), OLD)
    p["bob_list"] = deliver(os.path.join(b, "Lists", "Linux"), message("dev@lists.example", "[linux] patch", OLD, to="bob@home.example",
                                                                      extra={"List-Id": "Linux dev <linux.lists.example>"}), OLD)
    # Virtual-domain layout: root/<domain>/<user>/Maildir, with stray files beside the users.
    c = os.path.join(root, "example.org", "carol", "Maildir")
    p["carol_inbox"] = deliver(c, message("Pal <pal@gmail.com>", "hello carol", OLD, to="carol@example.org"), OLD)
    p["carol_migrated"] = deliver(os.path.join(c, ".MigratedMail.Inbox"), message("Pal <pal@gmail.com>", "old", OLD, to="carol@example.org"), OLD)
    with open(os.path.join(root, "example.org", "selector"), "w") as f:
        f.write("x")

    # tmp/ must be ignored
    with open(os.path.join(b, "tmp", "partial"), "wb") as f:
        f.write(b"From: x\n")
    return p


def corpus(root, n=12):
    """Clearly separable mail for model tests: store promos vs. notes from people. Returns relpaths."""
    d = os.path.join(root, "dave")
    ads, notes = [], []
    for i in range(n):
        when = OLD - timedelta(days=i * 7)
        ads.append(deliver(d, message(f"Deals <deals@store{i % 3}.example>", f"Huge sale {i}0% off everything",
                                      when, to="dave@home.example", extra=AD,
                                      body="Shop now! Limited time coupon, free shipping, unsubscribe here.\n"), when))
        notes.append(deliver(d, message(f"Pal {i} <pal{i}@gmail.com>", f"photos from the trip {i}", when,
                                        to="dave@home.example", body="Here are the pictures from the lake, love you all.\n"), when))
    rel = lambda p: os.path.relpath(p, root)
    return [rel(p) for p in ads], [rel(p) for p in notes]
