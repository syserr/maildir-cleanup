"""Terminal review: label individual messages junk or keep; the model retrains every page."""

import html
import os
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static

from . import model as ml
from .parse import open_message
from .pool import Pool

PAGE = 15


def M(template: str, **values) -> Content:
    """Markup for our own text only. Mail-derived text goes in as $values, which are never parsed."""
    return Content.from_markup(template, **values)


def one_line(s) -> str:
    return " ".join((s or "").split())


def mb(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:,.1f} MB"


def fmt_date(ts):
    return datetime.fromtimestamp(ts or 0, timezone.utc).strftime("%Y-%m-%d")


def read_body(path: str, limit=20000) -> str:
    try:
        with open_message(path) as f:
            msg = BytesParser(policy=policy.default).parse(f)
        head = "\n".join(f"{k}: {msg[k]}" for k in ("From", "To", "Date", "Subject", "List-Id") if msg[k])
        part = msg.get_body(preferencelist=("plain", "html"))
        text = part.get_content() if part else ""
        if part is not None and part.get_content_type() == "text/html":
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return f"{head}\n\n{text[:limit]}"
    except Exception as e:
        return f"(could not read {path}: {e})"


class MessageView(ModalScreen):
    BINDINGS = [("escape,o,q", "app.pop_screen", "close")]

    def __init__(self, path):
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(Content(read_body(self.path)))
        yield Footer()


def guess_text(p, threshold):
    """Red: the plan would delete it. Yellow: leaning junk but below the threshold."""
    if p is None:
        return Text("—", style="dim")
    if p >= 0.5:
        return Text(f"junk {p:.0%}", style="bold red" if p >= threshold else "yellow")
    return Text(f"keep {1 - p:.0%}", style="green" if p <= 0.1 else "cyan")


LABEL_STYLE = {"junk": "bold reverse red", "keep": "bold reverse green"}


class ReviewApp(App):
    CSS = """
    #status { padding: 0 1; height: auto; }
    DataTable { height: 17; }
    #preview-box { border-top: solid $accent; padding: 0 1; }
    MessageView { background: $background 90%; } MessageView VerticalScroll { padding: 1 2; }
    """
    BINDINGS = [
        ("j", "label('junk')", "junk"),
        ("k", "label('keep')", "keep"),
        Binding("J", "label_rest('junk')", "rest of page junk"),
        Binding("K", "label_rest('keep')", "rest of page keep"),
        ("n", "next_page", "next page (retrain)"),
        ("o", "open", "open full message"),
        ("u", "undo", "undo"),
        ("q", "quit", "quit"),
    ]

    def __init__(self, conn, cutoff_ts, mail_root, training, threshold=0.85, user=None, pool_size=20000, seed=None, grep=()):
        super().__init__()
        self.conn, self.cutoff_ts, self.mail_root = conn, cutoff_ts, mail_root
        self.threshold, self.user, self.training = threshold, user, training
        self.grep = list(grep)
        training.to_db(conn)
        self.pool = Pool(conn, cutoff_ts, size=pool_size, user=user, seed=seed, grep=self.grep)
        self.model = None
        self.rows = []          # pool indexes on this page
        self.marks = {}         # pool index -> label given this page
        self.history = []       # (pool index, page number)
        self.page_no = 0
        self.labelled = 0
        self.message = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="preview-box"):
            yield Static(id="preview")
        yield Footer()

    def on_mount(self):
        self.title = "maildir-cleanup review" + (f" [{self.user}]" if self.user else "") + \
            "".join(f' "{w}"' for w in self.grep)
        t = self.query_one(DataTable)
        for label, key, width in (("", "label", 6), ("model", "guess", 9), ("date", "date", 10), ("mailbox", "user", 18),
                                  ("from", "from", 30), ("subject", "subject", 70)):
            t.add_column(label, key=key, width=width)
        self.retrain()
        self.load_page()

    # --- model and pages -------------------------------------------------------------------

    def retrain(self):
        self.model = ml.train(self.conn, self.training)
        self.pool.score(self.model)

    def load_page(self):
        self.rows = self.pool.page(PAGE, self.threshold)
        self.marks = {}
        self.page_no += 1
        t = self.query_one(DataTable)
        t.clear()
        for i in self.rows:
            r = self.pool.rows[i]
            p = None if self.pool.p is None else float(self.pool.p[i])
            who = one_line(r["from_name"] or r["from_addr"])
            t.add_row(Text(""), guess_text(p, self.threshold), fmt_date(r["ts"]), Text(r["user"].split("/")[-1][:18]),
                      Text(who[:30]), Text(one_line(r["subject"])[:70]), key=str(i))
        if self.rows:
            t.move_cursor(row=0)
        self.render_status()
        self.render_preview()

    def render_status(self):
        n = self.training.counts()
        parts = [f"training set {n['junk'] + n['keep']:,} ([red]{n['junk']:,} junk[/] / [green]{n['keep']:,} keep[/]) · page {self.page_no}"]
        est = self.pool.estimate(self.threshold)
        if self.grep:
            parts.append(f"{self.pool.eligible_n:,} unlabelled old messages match " + " + ".join(f'"{w}"' for w in self.grep))
        if est:
            parts.append(f"model would delete ≈{est[0]:,} msgs / {mb(est[1])} of {self.pool.eligible_n:,} eligible "
                         f"({mb(self.pool.eligible_bytes)}) at ≥{self.threshold:.0%}")
        elif self.pool.p is None:
            parts.append(f"no model yet: label a few junk and a few keep, then n (deletes at ≥{self.threshold:.0%} junk)")
        mode = "least-sure first, plus spot checks" if self.pool.p is not None else "one per big sender"
        lines = [" · ".join(parts),
                 M("[dim]Is this message worth keeping once it's old? j junk · k keep · J/K rest of page · "
                   f"n next page ({mode})[/dim]   [b]$msg[/b]", msg=self.message)]
        self.query_one("#status", Static).update(Content("\n").join(x if isinstance(x, Content) else M(x) for x in lines))

    def current(self):
        t = self.query_one(DataTable)
        if not self.rows or t.cursor_row is None or t.cursor_row >= len(self.rows):
            return None
        return self.rows[t.cursor_row]

    def render_preview(self):
        box = self.query_one("#preview", Static)
        i = self.current()
        if i is None:
            box.update(M("[b]No more messages in this sample.[/b] Quit and run `maildir-cleanup train`, or review again."))
            return
        r, doc = self.pool.rows[i], self.pool.docs[i]
        lines = [
            M("[b]$who[/b]  <$addr>   [dim]$user / $folder · $date · $size[/dim]", who=one_line(r["from_name"]),
              addr=r["from_addr"] or "", user=r["user"], folder=r["folder"], date=fmt_date(r["ts"]), size=mb(r["size"] or 0)),
            M("[b]$s[/b]", s=one_line(r["subject"]) or "(no subject)"),
        ]
        total, sampled, share = self.pool.org_view(r["org"])
        sender = f"sender {r['org']}: {total:,} old unprotected messages"
        if share is not None:
            sender += f"; model calls {share:.0%} of the {sampled:,} sampled junk"
        lines.append(M("[dim]$s[/dim]", s=sender))
        if self.model:
            junk, keep = self.model.explain(doc)
            fmt = lambda xs: ", ".join(f"{t} {w:+.1f}" for t, w in xs) or "—"
            lines.append(M("[dim]why: junk ← [/dim][red]$j[/]   [dim]keep ← [/dim][green]$k[/]", j=fmt(junk), k=fmt(keep[:4])))
        lines += [Content(""), Content(r["text"] or "(no text extracted; o opens the full message)")]
        box.update(Content("\n").join(lines))
        self.query_one("#preview-box").scroll_home(animate=False)

    def on_data_table_row_highlighted(self, event):
        self.render_preview()

    # --- actions ------------------------------------------------------------------------------

    def _set(self, i, label):
        r = self.pool.rows[i]
        p = None if self.pool.p is None else float(self.pool.p[i])
        self.training.add(os.path.join(self.mail_root, r["relpath"]), label, dict(r), guess=p)
        self.marks[i] = label
        self.history.append((i, self.page_no))
        self.labelled += 1
        self.query_one(DataTable).update_cell(str(i), "label", Text(label, style=LABEL_STYLE[label]))

    def action_label(self, label):
        i = self.current()
        if i is None:
            return
        self._set(i, label)
        t = self.query_one(DataTable)
        if len(self.marks) == len(self.rows):
            self.action_next_page()
            return
        if t.cursor_row < len(self.rows) - 1:
            t.move_cursor(row=t.cursor_row + 1)
        self.message = ""
        self.render_status()

    def action_label_rest(self, label):
        for i in self.rows:
            if i not in self.marks:
                self._set(i, label)
        self.action_next_page()

    def action_next_page(self):
        self.message = f"page {self.page_no}: {len(self.marks)} labelled; retrained" if self.marks else "skipped page"
        self.retrain()
        self.load_page()

    def action_undo(self):
        if not self.history:
            return
        i, page = self.history.pop()
        r = self.pool.rows[i]
        self.training.remove(r["dir"], r["key"])
        self.labelled -= 1
        if page == self.page_no and i in self.marks:
            del self.marks[i]
            self.query_one(DataTable).update_cell(str(i), "label", Text(""))
            self.query_one(DataTable).move_cursor(row=self.rows.index(i))
            self.message = "undone"
        else:
            self.message = f"undid label on an earlier page: {r['subject'] or ''}"[:80]
        self.render_status()

    def action_open(self):
        i = self.current()
        if i is not None:
            self.push_screen(MessageView(os.path.join(self.mail_root, self.pool.rows[i]["relpath"])))


def run(conn, cutoff_ts, mail_root, training, threshold, user=None, grep=()):
    app = ReviewApp(conn, cutoff_ts, mail_root, training, threshold, user, grep=grep or ())
    app.run()
    return app.labelled
