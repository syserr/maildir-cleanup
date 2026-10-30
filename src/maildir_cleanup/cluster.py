"""Group messages into sender clusters, the unit a human reviews and labels."""

import re
from collections import Counter
from dataclasses import dataclass, field

# Senders at these domains are individuals, so group by address, not domain.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "bellsouth.net", "cox.net",
    "charter.net", "earthlink.net", "juno.com", "netzero.net", "mail.com", "gmx.com", "gmx.net",
    "protonmail.com", "proton.me", "qq.com", "yahoo.co.uk", "hotmail.co.uk", "btinternet.com",
}
# Mailing-list hosts carry many unrelated lists, so group by List-Id.
LIST_HOSTS = {"googlegroups.com", "groups.io", "yahoogroups.com", "freelists.org", "sourceforge.net", "lists.debian.org"}

ESP_HINTS = (
    "sendgrid", "mcsv.net", "rsgsv.net", "mailchimp", "mandrillapp", "exacttarget", "amazonses",
    "sparkpost", "mailgun", "constantcontact", "rsys", "responsys", "bounce", "cmail", "createsend",
    "emarsys", "marketo", "mktomail", "sailthru", "klaviyo", "bluehornet", "epsilon", "returnpath",
)
NOREPLY = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|newsletter|news|offers?|deals?|promo|marketing|info|updates?|mailer)@")


def cluster_key(from_addr: str, org: str, list_id: str) -> str:
    if org in LIST_HOSTS and list_id:
        return f"list_id:{list_id}"
    if org in FREEMAIL or not org:
        if list_id:
            return f"list_id:{list_id}"
        return f"address:{from_addr or '(none)'}"
    return f"domain:{org}"


def is_bulk(r) -> bool:
    rd = r["return_domain"] or ""
    return bool(
        r["list_unsub"] or r["bulk_hdrs"] or r["precedence"] in ("bulk", "list", "junk")
        or any(h in rd for h in ESP_HINTS)
    )


def normalize_subject(s: str) -> str:
    s = re.sub(r"^((re|fwd?|aw|sv)\s*:\s*)+", "", (s or "").lower())
    s = re.sub(r"\d+([.,:/-]\d+)*", "#", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Cluster:
    key: str
    n: int = 0
    bytes: int = 0
    n_old: int = 0
    bytes_old: int = 0
    deletable_old: int = 0       # old and not protected
    deletable_bytes: int = 0
    unprotected: int = 0         # any age, not protected
    unprotected_bytes: int = 0
    first: int | None = None
    last: int | None = None
    seen: int = 0
    protected: Counter = field(default_factory=Counter)
    bulk: int = 0
    unsub: int = 0
    listid: int = 0
    noreply: int = 0
    spam: int = 0
    junk: int = 0
    users: Counter = field(default_factory=Counter)
    addresses: Counter = field(default_factory=Counter)
    names: Counter = field(default_factory=Counter)
    templates: set = field(default_factory=set)
    recent: list = field(default_factory=list)   # (ts, subject, relpath), newest kept

    @property
    def kind(self):
        return self.key.split(":", 1)[0]

    @property
    def value(self):
        return self.key.split(":", 1)[1]

    def add(self, r, cutoff_ts):
        size = r["size"] or 0
        ts = r["ts"] or 0
        self.n += 1
        self.bytes += size
        if ts < cutoff_ts:
            self.n_old += 1
            self.bytes_old += size
            if not r["protect"]:
                self.deletable_old += 1
                self.deletable_bytes += size
        self.first = ts if self.first is None else min(self.first, ts)
        self.last = ts if self.last is None else max(self.last, ts)
        self.seen += "S" in r["flags"]
        if r["protect"]:
            self.protected[r["protect"]] += 1
        else:
            self.unprotected += 1
            self.unprotected_bytes += size
        self.bulk += is_bulk(r)
        self.unsub += bool(r["list_unsub"])
        self.listid += bool(r["list_id"])
        self.noreply += bool(NOREPLY.search(r["from_addr"] or ""))
        self.spam += bool(r["spam_flag"])
        self.junk += r["kind"] == "junk"
        self.users[r["user"]] += 1
        self.addresses[r["from_addr"]] += 1
        if r["from_name"]:
            self.names[r["from_name"]] += 1
        if len(self.templates) < 2000:
            self.templates.add(normalize_subject(r["subject"]))
        if len(self.recent) < 40 or ts > self.recent[-1][0]:
            self.recent.append((ts, r["subject"] or "", r["relpath"]))
            self.recent.sort(key=lambda x: -x[0])
            del self.recent[40:]

    def sample_subjects(self, k=8):
        out, seen = [], set()
        for ts, subj, _ in self.recent:
            t = normalize_subject(subj)
            if t not in seen:
                seen.add(t)
                out.append((ts, subj))
            if len(out) >= k:
                break
        return out


SELECT = """SELECT id, user, kind, flags, size, ts, from_addr, from_name, from_domain, org, subject,
    list_id, list_unsub, precedence, return_domain, bulk_hdrs, spam_flag, protect, relpath FROM messages"""


def build(conn, cutoff_ts, rules=None, unresolved_only=False, users=None, keyfn=None, where="", params=()) -> dict[str, Cluster]:
    """Cluster messages (excluding the users' own sent mail and drafts).

    unresolved_only skips messages a rule already decides.
    keyfn overrides the cluster key, e.g. to split a domain by address."""
    keyfn = keyfn or (lambda r: cluster_key(r["from_addr"], r["org"], r["list_id"]))
    clusters: dict[str, Cluster] = {}
    for r in conn.execute(f"{SELECT} WHERE kind NOT IN ('sent','drafts') {where}", params):
        if users and r["user"] not in users:
            continue
        if unresolved_only and rules and rules.resolve(r["user"], r["from_addr"], r["from_domain"], r["list_id"]):
            continue
        k = keyfn(r)
        c = clusters.get(k)
        if c is None:
            c = clusters[k] = Cluster(k)
        c.add(r, cutoff_ts)
    return clusters


def ranked(clusters: dict[str, Cluster]) -> list[Cluster]:
    return sorted(clusters.values(), key=lambda c: (-c.deletable_bytes, -c.n))
