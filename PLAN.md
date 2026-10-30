# maildir-cleanup: plan

**Status (2026-10-30):** after the first real review session, labelling moved from sender clusters to **individual messages** (junk/keep). A message-level classifier (hashed subject/body/sender tokens, logistic regression) learns from them, and domain-wide judgments come out of the model instead of rules fixed up front. The rest of this file is the original plan, kept for history: its cluster review, cluster model and project layout were replaced. The README describes how review, training and planning work now.

**Status (2026-10-30, later):** the training set moved out of `inv.db` into `training/`: full copies of labelled messages in `junk/` and `keep/` Maildirs plus `index.tsv`. The model learns only from it, so pulling fresh mail or cleaning up never loses labels. Models are versioned in `data/models/`. See README "How it works".

## Context
About 12–15 users have years of mail in Maildir directories, roughly 35 GB+. Most of the bulk is old, unremarkable mail: store ads, newsletters, notifications and spam. The goal is to remove that mail up to a cutoff date (likely now minus 1 year) and keep anything personal or meaningful. A clone of the Maildirs will be placed in this project directory for analysis. The live server is never touched by this tool.

**Output:** a plain list of Maildir files to delete. You can run it against the clone, or copy it to the mail host and run it there.

You stay in the loop by labelling *sender clusters*, not individual messages, in a terminal UI. Each label becomes a rule and also training data for a small local model, so less review is needed on each pass.

Conventions match the sibling `~/clone/unhoard` project: uv, Python 3.12, `src/` layout, SQLite inventory, pytest with synthetic fixtures, deterministic code first, and the tool only proposes. Python isn't on PATH, so uv provides it. The working docs (`PLAN.md`, `NOTES.md`, `rules.toml`, reports) live in the project root. Nothing is hidden in caches.

## Approach

### 1. Scan → SQLite inventory (`scan`)
- Walk `<root>/<user>/` and detect the layout: Maildir++ `.Folder` dirs, nested folders, `cur/new/tmp`, `dovecot-uidlist`, and gz/zstd-compressed messages. Record what was found in `NOTES.md`.
- For each message, parse **headers only** with the stdlib `email` parser (bounded read, so it's fast on 35 GB). Fields: user, folder, relpath, **stable key** (the filename before `:2,`), size, Date (falling back to file mtime), From address/domain, To/Cc, Subject, Message-ID, In-Reply-To/References, List-Id, List-Unsubscribe, Precedence, X-Spam-* / X-Spam-Status, Auto-Submitted, Return-Path, and the Maildir flags (Seen, Replied, Flagged).
- The scan is resumable (skips relpaths it already has) and runs in a process pool. It is read-only on the mail tree.

### 2. Features and protection signals (deterministic)
- **Bulk/ad signals:** List-Unsubscribe, List-Id, Precedence bulk/list, known ESP Return-Path domains (sendgrid, mailchimp, exacttarget…), noreply senders, high message volume from the sender, templated subjects (normalized digits/dates), and a spam-header score.
- **Protect signals** (never auto-delete): the user replied to the sender (sender appears in the user's Sent folders, or the user's message is in a thread via In-Reply-To), the message is Flagged or Replied, it sits in a user-created folder you mark as protected, the sender is in a small personal-correspondent set (low volume and two-way traffic), or it has attachments from a personal sender.
- **Spam:** messages in Junk/Spam folders, or with a spam score over a threshold, are deleted regardless of age. This is configurable.

### 3. Clusters (the unit of review)
- Messages are grouped by **sender domain → sender address** (with List-Id as an alternate key). Each cluster shows: users affected, count, total size, date span, read ratio, any replies, a sample of 8 subjects, and the bulk signals.
- Clusters are ranked by bytes reclaimable before the cutoff, so the largest gains come first.

### 4. Review TUI (`review`) using `textual`
- One cluster per screen. Keys: `a` ad/bulk → delete before cutoff · `s` spam → delete all · `k` keep · `p` protect (never delete) · `x` split (label individual addresses inside a domain) · `v` view a sample message · `u` undo · `q` quit.
- Labels are written to SQLite **and** appended to a human-readable `rules.toml` in the project root (e.g. `domain = "bedbathbeyond.com"  action = "ad"`). You can edit that file by hand.
- Rule scopes are global or per user (some users may want to keep a store's mail).

### 5. Learned model (`train` / `suggest`)
- scikit-learn logistic regression over cluster-level features plus TF-IDF of the sample subjects, trained on your labels. It predicts ad/spam/keep with a confidence for each unlabeled cluster.
- `review --suggested` shows the model's guess first, and pressing Enter accepts it. Above a high-confidence threshold *and* with no protect signals, clusters can be auto-labelled. Those auto-labels are listed in the report so you can spot-check them.
- Agreement between the model and your labels is tracked per round and printed after each `train`, so you can see whether it's learning what you're after.
- An LLM hook (the OpenAI-compatible endpoint from unhoard's `judge` role) is a later phase, used only for clusters the model is unsure about.

### 6. Plan and export (`plan`, `export`)
- `plan --cutoff 2025-10-30` computes the per-message decision: delete if the cluster is labelled ad (and the message is before the cutoff), or spam (any date), and no protect signal applies. Every deletion records the rule or model reason for it.
- `report` writes `reports/plan-YYYYMMDD.md` with per-user and per-folder counts, GB reclaimed, the top clusters, auto-labelled clusters, and protected exceptions.
- `export` writes `out/delete-YYYYMMDD.txt`, one relpath per line relative to the Maildir root (`user/.Folder/cur/<file>`). It also writes `out/delete-YYYYMMDD.keys.txt` (user/folder/stable-key). Use the keys file if a message's flag suffix might have changed on the live server since the clone (e.g. it was read). A tiny standalone `scripts/apply-list.sh` resolves the keys by glob (`cur/key:2,*`, `new/key`) under a given root. It defaults to a dry run, with `--move <quarantine>` or `--rm`. You can also feed the plain list to `xargs rm` yourself.

## Project layout
```
PLAN.md  README.md  NOTES.md  rules.toml  pyproject.toml  .gitignore
src/maildir_cleanup/{cli.py, scan.py, parse.py, features.py, cluster.py, model.py, tui.py, plan.py, export.py, db.py}
scripts/apply-list.sh
tests/{fixtures.py, test_scan.py, test_features.py, test_plan.py, test_export.py}
mail/   (the clone, gitignored)   data/inv.db (gitignored)   reports/   out/
```
Dependencies: `textual`, `scikit-learn`, `zstandard` (only if compressed mail is found), and `pytest` (dev). `git init` happens at setup. The clone, DB and `out/` are gitignored, so no mail content is committed.

## Milestones
1. Scaffold, scan, inventory and `stats` (per user/folder/year sizes). This is where we learn the real layout.
2. Features, clusters and `clusters --top N` (a read-only text view), so you can see the ad pattern before any labelling.
3. TUI review and `rules.toml`.
4. Plan, report and export, plus `apply-list.sh`. This is the first usable deletion list.
5. Model training and suggestions, then the auto-label threshold.
6. (Later) LLM judge for low-confidence clusters.

## Verification
- `uv run pytest`: synthetic Maildirs (multiple users, `.Folder`s, flag suffixes, Sent replies, List-Unsubscribe ads, spam folder, compressed messages) assert that scan counts match, protected mail never appears in the export, the cutoff is respected, and the keys file resolves after flag renames.
- On the real clone: `scan` → `stats` (sanity-check against `du`) → `clusters --top 50` → label about 30 clusters → `plan` → read the report → `export`. Then run `apply-list.sh` in dry-run mode against the clone to confirm every line resolves.
