# Working notes

Findings about the real mail clone and decisions made during review, kept generic here: no mailbox names, own domains or list names. Detailed notes are kept locally in `NOTES.local.md` (gitignored).

## Layout (first scan, 2026-10-30)
- Dovecot Maildir++ in a virtual-domain layout: `<domain>/<user>/Maildir`, 43 mailboxes across 11 domains. Users are named `domain/user`.
- Domain directories also hold `private`, `public` and `selector` files (probably DKIM keys). These are ignored.
- No compressed messages, no unparseable headers.
- Many mailboxes have an imported tree under `.MigratedMail.*` (Inbox, Sent, Sent_Messages, Archives.YYYY, Solicitations, Lists…). `MigratedMail.Inbox` counts as an inbox and `MigratedMail.Sent*` as sent.
- 437,139 messages, 27.9 GB. Inbox 17.1 GB, other folders 6.1 GB, sent 4.2 GB, trash 0.3 GB, junk 0.1 GB.
- The most recent year alone is 5.8 GB. With a 1-year cutoff, a lot of the bytes are recent and out of scope.
- Protected by signals: about 71k messages. Of those: sent 37.6k, correspondent 24.4k, thread 7.1k, replied 0.9k, drafts 0.6k, flagged 0.4k.
- Sizes are uneven: the three biggest mailboxes are 6.7, 3.5 and 3.4 GB. The largest has more than half its mail in an imported `MigratedMail.Inbox`; another has 122k messages, mostly mailing lists.

## Observations
- 34k sender clusters. The top ones are what we expected: investment newsletters, social networks, big retailers, email marketing services (Constant Contact and similar), and store mail.
- The users' own domains appear as senders too. That mail is mostly internal and personal, and already heavily protected.
- Mailing-list archives (single lists of 5k–16k messages) are a judgment call: old list traffic is probably safe to delete, but some lists are worth keeping.

## Decisions
- 2026-10-30: switched from sender rules to message labels. Reviewing sender clusters forced an all-or-nothing call per domain, but domains mix ads with records and account mail (a retailer's promo@ vs its invoices, a social network's friend posts vs account changes). Now each message is labelled junk/keep and the model generalises, using sender tokens among its features. The 6 early domain rules were dropped. `rules.toml` remains only for hand-written hard overrides.
- Domains mix ads and records (store-news@ vs auto-confirm@ at the same retailer). Handled by labelling messages rather than senders, plus subject-based protection for records (`keep_subject_patterns`), since the same sender can send both.
- Keep-subject protects *records* only. A first broad version protected 16k messages, including newsletters about tax, "Order Now" ads, and list threads about passwords. Ephemeral notices (shipped, sign-in, codes) were dropped on purpose. Mailing-list mail (List-Id), junk-folder mail and spam-flagged mail are never protected by subject. Result: 6,148 messages.
- Cutoff default: 365 days before the run date.
- Ads shared across many users are labelled once, globally.
- 2026-10-30: model deletes at junk score ≥ `junk_threshold` (config, set to 0.85). `--threshold 70` overrides per run; the plan report tabulates 50–95% to help pick it.
- 2026-10-30: training set separated from the clone. Labels used to be rows in `inv.db` pointing at messages in the clone, so a cleanup plus rescan would have silently dropped them, mostly the junk ones. Now each label copies the message into `training/junk` or `training/keep` (the folder is the label), and `training/index.tsv` records the origin. The model trains only from `training/`; `inv.db` is purely derived. Models are versioned in `data/models/` when the training set changes. Migrated 1,279 labels (1,044 junk / 235 keep, 64 MB).
- 2026-10-30: first model from `training/`: 86% cross-validated accuracy. Mailing-list traffic is still inconsistent (one list leans junk, another keep): check lists in the plan report before trusting it.
