"""Per-message protection signals derived from each user's own sent mail and flags."""

import re
from collections import Counter, defaultdict


def analyze(conn, protected_folders=(), keep_subject_patterns=()) -> Counter:
    """Set messages.protect to the reason a message must never be deleted, or NULL."""
    own = defaultdict(Counter)        # user -> addresses they send from
    sent_to = defaultdict(set)        # user -> addresses they have written to
    replied_ids = defaultdict(set)    # user -> Message-IDs their sent mail answers
    for r in conn.execute("SELECT user, from_addr, to_addrs, in_reply_to, refs FROM messages WHERE kind='sent'"):
        if r["from_addr"]:
            own[r["user"]][r["from_addr"]] += 1
        sent_to[r["user"]].update((r["to_addrs"] or "").split())
        replied_ids[r["user"]].update((r["in_reply_to"] or "").split())
        replied_ids[r["user"]].update((r["refs"] or "").split())
    for user, addrs in own.items():
        sent_to[user] -= set(addrs)

    protected_folders = set(protected_folders)
    keep_subject = re.compile("|".join(f"(?:{p})" for p in keep_subject_patterns), re.I) if keep_subject_patterns else None
    counts = Counter()
    updates = []
    rows = conn.execute("SELECT id, user, folder, kind, flags, from_addr, message_id, subject, spam_flag, list_id FROM messages")
    for r in rows:
        reason = None
        if r["kind"] in ("sent", "drafts"):
            reason = r["kind"]
        elif r["folder"] in protected_folders or f"{r['user']}:{r['folder']}" in protected_folders:
            reason = "folder"
        elif "F" in r["flags"]:
            reason = "flagged"
        elif "R" in r["flags"]:
            reason = "replied"
        elif r["message_id"] and r["message_id"] in replied_ids[r["user"]]:
            reason = "thread"
        elif r["from_addr"] and r["from_addr"] in sent_to[r["user"]]:
            reason = "correspondent"
        elif (keep_subject and r["kind"] != "junk" and not r["spam_flag"] and not r["list_id"]
              and keep_subject.search(r["subject"] or "")):
            # Receipts and account notices. Not in junk or spam-flagged mail, where phishing uses the same
            # words, and not mailing-list traffic, which merely discusses passwords and taxes.
            reason = "keep-subject"
        counts[reason] += 1
        updates.append((reason, r["id"]))
    conn.executemany("UPDATE messages SET protect=? WHERE id=?", updates)
    conn.commit()
    return counts
