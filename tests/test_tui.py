import asyncio
import os

from maildir_cleanup.tui import ReviewApp

from . import fixtures as fx
from .conftest import rescan


def labels(env):
    tr = env["training"]
    return {o: tr.label_of(*o) for o in tr.by_origin}


def test_label_messages_and_retrain(env):
    fx.corpus(env["root"])
    rescan(env)

    async def drive():
        app = ReviewApp(env["conn"], env["cfg"].cutoff_ts(), env["root"], env["training"], seed=1)
        async with app.run_test(size=(160, 50)) as pilot:
            assert app.pool.p is None and len(app.rows) > 2     # no labels yet: diverse cold-start page
            await pilot.press("j")
            await pilot.press("k")
            assert len(labels(env)) == 2
            await pilot.press("u")
            assert len(labels(env)) == 1
            await pilot.press("k", "J")                      # rest of the page as junk, then next page
            assert app.page_no == 2 and app.model is not None and app.pool.p is not None
            await pilot.press("o")
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(drive())
    got = labels(env)
    assert set(got.values()) == {"junk", "keep"} and list(got.values()).count("keep") == 1


def test_markup_in_mail_is_shown_literally(env):
    nasty = "[b]Deal[/b] [link=x $5 [/] https://x.example/?ref_=a\n[ref=y] [@click=quit] \\[esc]"
    d = os.path.join(env["root"], "erin")
    fx.deliver(d, fx.message("[odd] <ads@markup.example>", nasty.splitlines()[0], fx.OLD, extra=fx.AD, body=nasty * 50), fx.OLD)
    rescan(env)
    assert env["conn"].execute("SELECT from_addr FROM messages WHERE user='erin'").fetchone()[0] == "ads@markup.example"

    async def drive():
        app = ReviewApp(env["conn"], env["cfg"].cutoff_ts(), env["root"], env["training"], user="erin")
        async with app.run_test(size=(160, 50)) as pilot:
            assert len(app.rows) == 1
            app.render_preview()
            await pilot.press("o")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(drive())


def test_grep_limits_review_to_matching_mail(env):
    from maildir_cleanup.pool import Pool

    fx.corpus(env["root"])
    rescan(env)
    cutoff = env["cfg"].cutoff_ts()
    lake = Pool(env["conn"], cutoff, grep=["LAKE"])                  # body text, any case
    assert lake.eligible_n == 12 and all("lake" in r["text"] for r in lake.rows)
    assert Pool(env["conn"], cutoff, grep=["store1.example"]).eligible_n == 4    # sender address
    assert Pool(env["conn"], cutoff, grep=["lake", "photos from the trip 3"]).eligible_n == 1   # all words must match
    assert Pool(env["conn"], cutoff, grep=["no-such-word"]).eligible_n == 0

    async def drive():
        app = ReviewApp(env["conn"], cutoff, env["root"], env["training"], seed=1, grep=["lake"])
        async with app.run_test(size=(160, 50)) as pilot:
            assert app.rows and all("lake" in app.pool.rows[i]["text"] for i in app.rows)
            await pilot.press("q")

    asyncio.run(drive())
