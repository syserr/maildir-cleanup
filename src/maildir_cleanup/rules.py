"""Optional hand-written overrides in rules.toml: always protect, keep, or delete mail from an address,
List-Id or domain. Most decisions come from the training set and the model instead."""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

ACTIONS = {
    "ad": "bulk/ads: delete before the cutoff",
    "spam": "spam: delete at any age",
    "keep": "not bulk (junk/trash folder cleanup still applies)",
    "protect": "never delete anything matching",
}
KINDS = ("address", "list_id", "domain")

HEADER = """\
# maildir-cleanup rules: optional hard overrides, edited by hand. Usually empty: most decisions come
# from your labels in training/ and the model. Use a rule only for mail that must always be treated one way.
# Put remarks in a `note` field rather than comments, in case the file is rewritten by Rules.save().
#
# Each [[rule]] matches exactly one of:
#   address = "deals@store.example"   (sender address)
#   list_id = "news.store.example"    (List-Id header)
#   domain  = "store.example"         (sender domain, including subdomains)
# action:  ad | spam | keep | protect
# user:    optional; limits the rule to one mailbox. User rules beat global ones.
# Most specific wins: address, then list_id, then domain.
"""


@dataclass
class Rule:
    kind: str
    value: str
    action: str
    user: str | None = None
    note: str | None = None

    def describe(self) -> str:
        scope = f" [{self.user}]" if self.user else ""
        return f"{self.kind}={self.value} {self.action}{scope}"


class Rules:
    def __init__(self, path):
        self.path = Path(path)
        self.items: list[Rule] = []
        if self.path.exists():
            for d in tomllib.loads(self.path.read_text()).get("rule", []):
                kinds = [k for k in KINDS if k in d]
                if len(kinds) != 1 or d.get("action") not in ACTIONS:
                    raise SystemExit(f"{self.path}: bad rule {d}")
                self.items.append(Rule(kinds[0], str(d[kinds[0]]).lower(), d["action"], d.get("user"), d.get("note")))
        self._index()

    def _index(self):
        self.by = {(r.kind, r.value, r.user): r for r in self.items}

    def add(self, rule: Rule) -> Rule | None:
        """Add or replace the rule for this match. Returns the rule it replaced."""
        rule.value = rule.value.lower()
        old = self.by.get((rule.kind, rule.value, rule.user))
        if old:
            self.items.remove(old)
        self.items.append(rule)
        self._index()
        return old

    def remove(self, rule: Rule):
        self.items.remove(rule)
        self._index()

    def save(self):
        out = [HEADER]
        for r in self.items:
            out.append("\n[[rule]]")
            out.append(f"{r.kind} = {json.dumps(r.value)}")
            out.append(f"action = {json.dumps(r.action)}")
            if r.user:
                out.append(f"user = {json.dumps(r.user)}")
            if r.note:
                out.append(f"note = {json.dumps(r.note)}")
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(out) + "\n")
        tmp.replace(self.path)

    def resolve(self, user, from_addr, from_domain, list_id) -> Rule | None:
        for u in (user, None):
            if from_addr and (r := self.by.get(("address", from_addr, u))):
                return r
            if list_id and (r := self.by.get(("list_id", list_id, u))):
                return r
            d = from_domain or ""
            while d.count(".") >= 1:
                if r := self.by.get(("domain", d, u)):
                    return r
                d = d.split(".", 1)[1]
        return None
