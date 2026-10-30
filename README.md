# maildir-cleanup

Finds old bulk mail (store ads, newsletters, notifications) and spam in a set of users' Maildirs and writes a **list of files to delete**. You label individual messages *junk* or *keep* in a terminal UI. A local classifier learns from their content (subject, body, sender and bulk-mail headers) and scores every other message. You never commit a whole sender up front. If everything from a domain is junk, the model learns that from your labels, and keeps showing you that domain's messages whenever it's unsure.

The tool only reads mail. It never deletes anything. You apply the list yourself, against the clone or on the mail server.

See [PLAN.md](PLAN.md) for the design, and [NOTES.md](NOTES.md) for what we have learned about the real data.

## How it works

The project keeps two kinds of data apart: **sources of truth**, which only your decisions produce, and **derived data**, which can be deleted and rebuilt at any time.

| source of truth (back these up) | derived (safe to delete and rebuild) |
|---|---|
| `training/`: copies of every message you labelled, in `junk/` and `keep/` | the mail clone (rsync it again) |
| `config.toml`, `rules.toml` | `data/inv.db`, the inventory (`scan`) |
| | `data/models/`, the models (`train`) |
| | `reports/`, `out/`: plans and delete lists (`plan`, `export`) |

Data flows one way:

```
mail server ──rsync──▶ clone ──scan──▶ inventory ──review──▶ you label a message
                                           │                         │ copied into
                                           │                         ▼
                                           │                  training/junk, training/keep
                                           │                         │ train (reads only training/)
                                           ▼                         ▼
                                         plan ◀──────────────────  model (dated, kept in data/models/)
                                           │
                                           ▼
                             delete list ──▶ quarantine on the server ──▶ delete
```

**The training set is a folder of example emails sorted into junk and keep.** The model learns from that folder and nothing else. Labelling *copies* the message into it, so refreshing or cleaning up the clone never changes what the model was taught. Each new pull brings new mail to review, and new labels are added on top of the old ones.

- An example's label is the folder it's in. Moving a file between `training/junk/cur` and `training/keep/cur` relabels it, and you can do that in a mail client (`mutt -f training/junk`).
- `training/index.tsv` records where each label came from: mailbox folder, filename key, folder kind, when, and the model's guess at the time. `plan` uses it to apply your label to that exact message while it still exists, and `review` uses it to skip mail you've already labelled.
- The same message delivered to several mailboxes (same Message-ID) is stored once.

**Models are versioned.** `train` (and `plan`) save `data/models/model-YYYYMMDD-HHMMSS.pkl` with a `.json` describing it, but only when the training set has changed. Nothing is overwritten. `train` compares each new model with the previous one, so a change in behaviour shows up before you delete anything. The plan report names the model it used.

### Refreshing from the server

```
rsync -a --delete mailhost:/path/to/domains/ /path/to/clone/     # the clone mail_root points at
uv run maildir-cleanup scan          # adds new mail, drops what's gone
uv run maildir-cleanup review        # label some of the new mail
uv run maildir-cleanup train         # new model version, compared with the last
uv run maildir-cleanup plan
uv run maildir-cleanup export
```

`--delete` matters. Without it, mail deleted on the server (by a cleanup, or by its user) stays in the clone and is planned again, and messages whose flags changed appear twice.

## Workflow

Requires [uv](https://docs.astral.sh/uv/). Start with `cp config.example.toml config.toml` and set `mail_root` to your Maildir clone (default `mail/`), or pass `--mail-root DIR` (or `-m DIR`) before the command, e.g. `uv run maildir-cleanup -m /tank/mailclone scan`. Under the root, each user is a Maildir, or contains a `Maildir/` subdirectory, either directly (`<user>/`) or one level down (`<domain>/<user>/`, Dovecot's virtual-domain layout). `config.toml` is gitignored, since it holds your local paths.

```
uv run maildir-cleanup scan          # headers, body text, protection signals (resumable; rerun after re-syncing)
uv run maildir-cleanup stats         # sizes by user, folder kind, year
uv run maildir-cleanup review        # label messages junk/keep; the model retrains every page
uv run maildir-cleanup train         # accuracy on training/, what it learned; saves and compares model versions
uv run maildir-cleanup plan          # decide; writes reports/plan-YYYYMMDD.md
uv run maildir-cleanup export        # writes out/delete-YYYYMMDD.txt and .keys.txt
```

Repeat review → train → plan until the report looks right. `--cutoff YYYY-MM-DD` overrides the default cutoff (today minus `cutoff_days`). `junk_threshold = 0.85` in `config.toml` sets how high the model's junk score must be before it deletes (here 85%); `plan --threshold 80` overrides it for one run. The plan report has a table of what 50–95% would each delete. `review --grep webinar` (or `-g`, repeatable: all words must match) reviews only mail containing a word in the subject, sender, recipients, list or body. `score FILE...` shows one message's junk score, the words behind it, and what `plan` would do with it. `clusters --subjects 3` gives an overview of the biggest senders.

### Reviewing

Each message gets one question: *is it worth keeping once it's old?* The review shows a page of 15 messages from the mail that is actually up for deletion: older than the cutoff, not protected, and not in Junk/Trash. The highlighted message's text appears below the list, along with:
- what the model currently thinks of its sender domain overall;
- which tokens push it toward junk or keep (`@org:…` = sender domain, `@unsubscribe` = has an unsubscribe header, `s:…` = subject word).

Before the model exists, a page holds one message from each of the biggest senders. Once you have labelled a few of each kind, pages hold the messages the model is least sure about (at most 2 per sender), plus 4 confident ones as spot checks.

| key | |
|---|---|
| `j` / `k` | junk / keep, then move down. Labelling the last row moves to the next page. |
| `J` / `K` | label every unlabelled row on the page, then next page |
| `n` | next page (retrains the model) |
| `o` | open the full message |
| `u` | undo the last label |
| ↑ ↓ | move |
| `q` | quit. Labels are saved as you go. |

`review --user NAME` shows messages from one mailbox only. Each label copies the message into `training/` straight away; `u` removes it again.

### How a message is decided

In order, the first match wins:
1. Protection signals (below): kept.
2. `rules.toml` overrides, if you write any: `protect`/`keep`/`ad`/`spam` for an address, domain or List-Id.
3. Your own label on that message: junk deletes it if it is older than the cutoff, keep keeps it.
4. The model: if it is older than the cutoff and the junk probability is at least the threshold, it is deleted.
5. Folder cleanup: Junk/Spam folders (`junk_any_age`), Trash older than the cutoff (`trash_before_cutoff`), and old mail the server's spam filter flagged (`spam_header_before_cutoff`).

Recent mail is only ever deleted by folder cleanup or a `spam` rule.

### What is never deleted

These are kept regardless of any rule:
- Sent mail and drafts.
- Flagged messages, and messages marked as replied to.
- Messages in a thread the user replied to.
- Mail from anyone the user has written to.
- Mail in `protected_folders`.
- Mail matching a `protect` rule in `rules.toml`.
- Mail whose subject looks like a record: a receipt, invoice, statement, order confirmation or tax form (`keep_subject_patterns`). Short-lived notices like shipped, sign-in alerts and verification codes are not protected.

### Applying the list

`out/delete-*.txt` holds paths relative to the mail root. If the live mailboxes have changed since the clone (for example, messages were read, so their filename flags changed), use `out/delete-*.keys.txt` instead:

```
scripts/apply-list.sh /path/to/mailroot out/delete-YYYYMMDD.keys.txt                    # dry run
scripts/apply-list.sh /path/to/mailroot out/delete-YYYYMMDD.keys.txt --move /quarantine # undoable
scripts/apply-list.sh /path/to/mailroot out/delete-YYYYMMDD.keys.txt --rm
```

Dovecot rebuilds its indexes when message files disappear.

## Development

```
uv run pytest
```

Tests use synthetic Maildirs from `tests/fixtures.py`. `mail/`, `data/`, `training/`, `reports/` and `out/` are gitignored because they contain mail or mail metadata.
