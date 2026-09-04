#!/usr/bin/env python3
"""
Keep latest.json current, from GitHub Actions, with nothing of the author's
switched on.

WHAT THIS REPLACED
------------------
The chain used to be: pieface scrapes -> Syncthing carries it to a Mac -> the
Mac runs `npm run publish-draws:push` by hand -> the app sees it. Two machines
had to be awake and one step was manual, so results reached phones whenever
somebody sat down, not when they were published.

This script is the same scrape with the two machines taken out. The pieface
updater still writes the Obsidian vault whenever it happens to be running;
that is now an independent job, not a link in this chain.

ONE PARSER, NOT TWO
-------------------
Singapore Pools' markup is the fragile part, so it is deliberately NOT
reimplemented here. `scraper.py` is a byte-for-byte copy of the pieface
updater's `update_4d_toto.py`, imported for its `scrape_*` functions; this
file only decides what to fetch and how to write it out. Re-copy that file
when the pieface one changes — `--check-scraper` fails the build if the copy
has drifted from the checksum recorded in scraper.sha256.

BEING QUIET ABOUT IT
--------------------
Two things keep the request count near zero:

  1. The cheap check first. Singapore Pools publishes small pre-rendered
     fragments under /DataFileArchive/Lottery/Output/ — the same files their
     own site's JavaScript polls. Those say what the newest draw number is, so
     a run with no new draw costs two of them (~50KB) and stops.
  2. Only then the result pages, one per genuinely new draw, 1.5s apart, and
     never more than MAX_NEW per game per run.

A normal week is three 4D runs and two TOTO runs, each fetching one draw. That
is a few dozen requests a week, which is less than one person refreshing the
results page on a draw night.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
LATEST = HERE / "latest.json"
SCRAPER = HERE / "scraper.py"
SCRAPER_SUM = HERE / "scraper.sha256"

SCHEMA_VERSION = 1
SGT = timezone(timedelta(hours=8))

# How much history latest.json carries. The app bundles the full archive and
# lays this over the top, so this only has to cover "since the last release" —
# a year is generous and keeps the file around 50KB.
WINDOW_DAYS = 400

# Per game, per run. A real catch-up after a long outage is a handful; this is
# only here so a parser fault cannot turn into hundreds of requests.
MAX_NEW = int(os.environ.get("HOBEH_MAX", "10"))


def load_scraper():
    """Import the vendored scraper for its parsing functions."""
    import importlib.util

    # It reads HOBEH_DIR at import time for the vault it writes on the Pi.
    # Nothing here calls those paths, but the variable has to exist.
    os.environ.setdefault("HOBEH_DIR", str(HERE / ".unused"))
    spec = importlib.util.spec_from_file_location("hobeh_scraper", SCRAPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_scraper_matches() -> bool:
    """Has the vendored copy drifted from the checksum committed beside it?"""
    if not SCRAPER_SUM.exists():
        print("scraper.sha256 missing — cannot verify the vendored scraper")
        return False
    expected = SCRAPER_SUM.read_text().split()[0].strip()
    actual = hashlib.sha256(SCRAPER.read_bytes()).hexdigest()
    if expected != actual:
        print(f"scraper.py has drifted\n  expected {expected}\n  actual   {actual}")
        return False
    print("scraper.py matches its checksum")
    return True


def read_latest() -> dict:
    if not LATEST.exists():
        return {"version": SCHEMA_VERSION, "fourD": [], "toto": [], "upcoming": {}}
    data = json.loads(LATEST.read_text())
    if data.get("version") != SCHEMA_VERSION:
        raise SystemExit(f"latest.json is version {data.get('version')}, expected {SCHEMA_VERSION}")
    return data


def newest_draw_no(rows: list) -> int:
    numbers = [r["draw_no"] for r in rows if isinstance(r.get("draw_no"), int)]
    return max(numbers) if numbers else 0


def trim(rows: list) -> list:
    """Newest first, inside the window, one row per draw date."""
    cutoff = (datetime.now(SGT) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    seen: set[str] = set()
    out = []
    for row in sorted(rows, key=lambda r: r["draw_date"], reverse=True):
        if row["draw_date"] < cutoff or row["draw_date"] in seen:
            continue
        seen.add(row["draw_date"])
        out.append(row)
    return out


# ─── the scraper's shape is not the app's shape ──────────────────────────────
# The scraper speaks the vault's language, because that is what it was written
# for: `draw_date` is "Wed, 19 Aug 2026" and `draw_no` is a string. latest.json
# is read by the app, whose validator (parse() in src/data/remoteDraws.ts)
# requires an ISO date and drops — silently, row by row — anything else. Ship
# the raw rows and every new draw disappears with no error anywhere.
#
# This is the same conversion publish-draws.js did on the Mac; it has to live
# here now that the Mac is out of the loop.

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAY_NAMES = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
             "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday",
             "Sun": "Sunday"}


def parse_draw_date(raw) -> tuple[str, str] | None:
    """"Wed, 19 Aug 2026" -> ("2026-08-19", "Wednesday")."""
    m = re.fullmatch(r"(\w{3}),\s*(\d{1,2})\s+(\w{3})\s+(\d{4})", str(raw).strip())
    if not m:
        return None
    day_abbr, day, mon_abbr, year = m.groups()
    if mon_abbr not in MONTHS:
        return None
    return f"{year}-{MONTHS.index(mon_abbr) + 1:02d}-{int(day):02d}", DAY_NAMES.get(day_abbr, "")


def money(raw) -> float | None:
    m = re.search(r"\$?([\d,]+(?:\.\d+)?)", str(raw))
    return float(m.group(1).replace(",", "")) if m else None


def normalise(row: dict, game: str) -> dict | None:
    """One scraped draw in the shape latest.json publishes."""
    when = parse_draw_date(row.get("draw_date"))
    if not when:
        print(f"{game}: unreadable date {row.get('draw_date')!r} — row dropped")
        return None
    date, day = when
    try:
        draw_no = int(row["draw_no"])
    except (KeyError, TypeError, ValueError):
        print(f"{game}: unreadable draw number {row.get('draw_no')!r} — row dropped")
        return None

    out: dict = {"draw_no": draw_no, "draw_date": date, "day": day}
    if game == "4D":
        out.update({
            "first": row["first"], "second": row["second"], "third": row["third"],
            "starters": row["starters"], "consolations": row["consolations"],
        })
    else:
        out.update({"numbers": row["numbers"], "additional": row["additional"]})
    return out


def toto_shares(snapshot: dict) -> tuple[str, list, float | None] | None:
    """
    The newest TOTO draw's prize table, which only the snapshot carries.

    `scrape_toto_draw` returns the numbers and nothing else, so without this the
    freshest draw — the one everybody opens the app to see — would show no prize
    table at all, which the app words as "not published".
    """
    when = parse_draw_date((snapshot.get("latest_draw") or {}).get("draw_date"))
    if not when:
        return None

    shares = []
    for row in snapshot.get("winning_shares") or []:
        found = re.search(r"(\d+)", str(row.get("prize_group") or ""))
        if not found:
            continue
        group = int(found.group(1))
        if not 1 <= group <= 7:
            continue
        # Singapore Pools prints "-" in both columns for a group nobody won.
        # The archive's convention for that is zero/zero — NOT an absent row,
        # which the app reads as "not published" and words differently.
        amount = 0 if row.get("share_amount") == "-" else money(row.get("share_amount"))
        winners = 0 if row.get("no_of_winning_shares") == "-" else money(row.get("no_of_winning_shares"))
        if amount is None or winners is None:
            continue
        shares.append({"group": group, "winners": int(winners), "amount": int(amount)})

    if not shares:
        return None
    shares.sort(key=lambda s: s["group"])
    # The Group 1 pool is NOT winners x amount and cannot be recovered from the
    # table: in a draw nobody won, every share figure is zero while the pool is
    # still the headline number on the printed page.
    pool = money((snapshot.get("latest_draw") or {}).get("group_1_prize"))
    return when[0], shares, pool


def due_games(data: dict, today: str) -> list[str]:
    """
    Which games could still publish a result today.

    This is what lets the job poll every five minutes without hammering
    anybody: the moment today's draw has been collected, every remaining run
    of the evening answers "nothing due" and exits before opening a single
    connection. On a night with no draw at all, that is every run.

    The authority is `upcoming.drawDate`, which was scraped from Singapore
    Pools rather than derived from the Wed/Sat/Sun and Mon/Thu pattern — that
    pattern is a fallback that special and cascade draws break, and a cascade
    TOTO draw lands on a Friday about once every twenty-four draws.

    `expected <= today` rather than `==` so a draw the job was down for stays
    due instead of being skipped for ever.

    A TOTO DRAW IS NOT FINISHED WHEN ITS NUMBERS ARRIVE
    ---------------------------------------------------
    Singapore Pools publishes the winning numbers first and the prize table
    some minutes later. Treating "there is a row for today" as done for the day
    therefore stopped the evening at the numbers: draw 4211 was collected at
    7.03pm with no `winning_shares` on the page yet, every remaining run
    answered "nothing due" without opening a connection, and the prize table
    that went up shortly afterwards was never fetched at all. The app showed
    the newest draw with no group prize and no shares — the one thing on Latest
    Draw people open it for — and the strip below had already dropped the
    previous draw's table, so there was nothing to fall back to either.

    So TOTO stays due until the row for today carries `shares`. Those extra
    runs cost one pre-rendered fragment each, only on the evening of a TOTO
    draw, and only in the gap between the numbers and the table.
    """
    due = []
    for game, key in (("4D", "fourD"), ("TOTO", "toto")):
        today_row = next(
            (row for row in data.get(key) or [] if row["draw_date"] == today), None
        )
        if today_row is not None:
            # Collected. Done, unless the TOTO prize table is still to come.
            if game == "TOTO" and not today_row.get("shares"):
                due.append(game)
            continue
        expected = ((data.get("upcoming") or {}).get(key) or {}).get("drawDate")
        if not expected or expected <= today:
            due.append(game)
    return due


def collect(game: str, existing: list, scraper, session) -> tuple[list, dict | None]:
    """
    Fetch draws newer than what is already published.

    Returns the new rows and the snapshot they were decided from. The snapshot
    is handed back rather than dropped because the TOTO prize table lives in it
    too, and re-fetching the same fragment a second time in one run doubles the
    cost of exactly the runs `due_games` now adds — the evening ones waiting
    for the table to go up.
    """
    scrape_draw = scraper.scrape_4d_draw if game == "4D" else scraper.scrape_toto_draw
    scrape_snapshot = (
        scraper.scrape_4d_snapshot if game == "4D" else scraper.scrape_toto_snapshot
    )

    have = newest_draw_no(existing)
    if not have:
        raise SystemExit(f"{game}: latest.json carries no numbered draw to resume from")

    # The cheap check. If the snapshot's newest draw is one we already have,
    # this run is over without touching a single result page.
    snapshot = scrape_snapshot(session)
    published = None
    if snapshot:
        latest_draw = snapshot.get("latest_draw") or {}
        try:
            published = int(latest_draw.get("draw_no"))
        except (TypeError, ValueError):
            published = None

    if published is not None and published <= have:
        print(f"{game}: up to date at draw {have}")
        return [], snapshot

    target = published if published is not None else have + MAX_NEW
    print(f"{game}: have {have}, published {published} — fetching forward")

    added = []
    for draw_no in range(have + 1, min(target, have + MAX_NEW) + 1):
        row = scrape_draw(draw_no, session)
        if not row:
            # Not published yet, or the markup moved. Either way stop and let
            # the next run resume rather than hammering through the gap.
            print(f"{game}: draw {draw_no} not available — stopping here")
            break
        clean = normalise(row, game)
        if not clean:
            break
        added.append(clean)
        print(f"{game}: + draw {draw_no} ({clean['draw_date']})")
        if draw_no < target:
            import time

            time.sleep(scraper.DELAY)
    return added, snapshot


def upcoming_block(game: str, scraper, session, previous: dict | None) -> dict | None:
    """
    The next draw, and for TOTO its estimated jackpot.

    The field names are remapped rather than passed through. The scraper speaks
    `date` / `time_24h` / `next_jackpot`; the app reads `drawDate` / `drawTime`
    / `jackpot` (see parseUpcoming in src/data/remoteDraws.ts). Emitting the
    scraper's own names would leave the jackpot card and the next-draw date
    silently blank — the shape below is exactly what publish-draws.js produced,
    so the app cannot tell which of the two wrote the file.
    """
    scrape_snapshot = (
        scraper.scrape_4d_snapshot if game == "4D" else scraper.scrape_toto_snapshot
    )
    try:
        snapshot = scrape_snapshot(session) or {}
    except Exception as exc:  # noqa: BLE001 - a missing estimate is not fatal
        print(f"{game}: could not read the next-draw snapshot ({exc})")
        return previous

    next_draw = snapshot.get("next_draw") or {}
    date = next_draw.get("date") or ""
    time_24h = next_draw.get("time_24h") or ""

    out: dict = {}
    raw = next_draw.get("raw")
    if isinstance(raw, str):
        out["raw"] = raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        out["drawDate"] = date
    if re.fullmatch(r"\d{2}:\d{2}", time_24h):
        out["drawTime"] = time_24h
    if isinstance(snapshot.get("scraped_at"), str):
        out["scrapedAt"] = snapshot["scraped_at"]

    if game == "TOTO" and isinstance(snapshot.get("next_jackpot"), str):
        # "$4,500,000 est" -> "$4,500,000". The card adds its own "est."
        # suffix, and prints 预估 in Chinese, so the English word must not
        # ride along inside the figure.
        amount = re.search(r"\$[\d,]+(?:\.\d+)?", snapshot["next_jackpot"])
        if amount:
            out["jackpot"] = amount.group(0)

    if not out.get("drawDate"):
        print(f"{game}: snapshot carried no usable next-draw date")
    if game == "TOTO" and not out.get("jackpot"):
        print("TOTO: snapshot carried no usable jackpot figure")

    if not (out.get("drawDate") or out.get("jackpot")):
        return previous
    return out


def meaningful(block: dict | None) -> dict | None:
    """`upcoming` minus the timestamp, for deciding whether anything changed.

    `scrapedAt` moves on every single run by definition. Comparing it would
    make every run a change, and this job runs on a timer — the repository
    would fill with commits that publish nothing.
    """
    if not block:
        return block
    return {k: v for k, v in block.items() if k != "scrapedAt"}


def run_pass(args) -> bool:
    """
    One look at Singapore Pools: scrape whatever is due, rewrite latest.json if
    anything changed.

    Returns True when the file was written. Nothing here commits or pushes —
    `publish()` does that, and `watch()` decides when.
    """
    data = read_latest()
    # What the app is already being served. A TOTO draw that is in here has
    # been published as a whole draw and must never be taken back off; one that
    # is not is a draw this run collected, and is subject to the completeness
    # gate below.
    already_published = {row["draw_date"] for row in data.get("toto") or []}
    today = datetime.now(SGT).strftime("%Y-%m-%d")
    games = ["4D", "TOTO"] if args.game == "all" else [args.game]

    # Checked before the scraper is even imported, let alone a socket opened.
    if not args.force:
        games = [g for g in games if g in due_games(data, today)]
        if not games:
            print(f"nothing due on {today} — no request made")
            return False
    print(f"due today ({today}): {', '.join(games)}")

    scraper = load_scraper()
    session = requests.Session()

    def payload(d: dict) -> str:
        """
        Everything in the file except the timestamp on it.

        `changed` used to be a flag half a dozen branches had to remember to
        set, and the withholding above broke it: a run that collects a TOTO
        draw and then holds it back has added rows to `data` and published
        nothing, so the flag said "changed" and the job stamped a new
        `generated`, committed a byte-identical file and busted every phone's
        cache for it. Comparing what actually goes into the file cannot drift
        from what the file contains, because it *is* what the file contains.
        """
        return json.dumps(
            {k: v for k, v in d.items() if k != "generated"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    published_before = payload(data)
    # Set when a TOTO draw is being held back, so the rest of the TOTO block
    # holds back with it. See the withholding note below.
    toto_withheld = False

    for game in games:
        key = "fourD" if game == "4D" else "toto"
        added, snapshot = collect(game, data[key], scraper, session)
        if added:
            data[key] = trim(data[key] + added)

        # TOTO prize tables live only in the snapshot, and only for the newest
        # draw. Attached after the merge so it finds the row wherever it landed.
        if game == "TOTO":
            if snapshot is None:
                # `collect` could not read the fragment. Try once more rather
                # than silently publishing a draw with no prize table.
                try:
                    snapshot = scraper.scrape_toto_snapshot(session) or {}
                except Exception as exc:  # noqa: BLE001
                    print(f"TOTO: could not read the prize table ({exc})")
                    snapshot = {}
            parsed = toto_shares(snapshot)
            newest = max((r["draw_date"] for r in data["toto"]), default=None)
            if parsed:
                date, shares, pool = parsed
                for row in data["toto"]:
                    if row["draw_date"] != date:
                        continue
                    row["shares"] = shares
                    if pool is not None:
                        row["group_1_prize"] = int(pool)
                    break

            # ── A TOTO DRAW IS PUBLISHED WHOLE OR NOT AT ALL ──────────────
            #
            # Singapore Pools puts the winning numbers up several minutes
            # before the prize table, and this used to forward each half the
            # moment it landed: one commit carrying a draw with no group prize
            # and no winning shares, then a second commit some minutes later
            # filling them in. Two publishes, and the first of them a draw that
            # is not finished being a draw.
            #
            # That is not a cosmetic problem. The app treats the newest row as
            # the latest draw the instant it arrives — it opens Latest Draw on
            # it and fires "the TOTO results are out" — so the first publish
            # sent every phone a notification about a result whose prize table
            # did not exist yet, and the user who tapped it got a Latest Draw
            # with the payout half of the screen missing.
            #
            # So a newly collected TOTO draw is WITHHELD until its prize table
            # is attached. It stays out of the published file, `due_games` sees
            # no row for today and keeps the game due, and a later run in the
            # evening collects the numbers and the table together and publishes
            # them as one update. The cost is re-fetching one result page on
            # each run in the gap, which is only ever the evening of a TOTO
            # draw and only ever the minutes between the two halves.
            withheld = [
                row
                for row in data["toto"]
                if row["draw_date"] not in already_published and not row.get("shares")
            ]
            if withheld:
                dates = ", ".join(row["draw_date"] for row in withheld)
                print(
                    f"TOTO: WITHHELD {dates} — numbers are up but the prize "
                    "table is not. Nothing published; the next run will try "
                    "again and publish the draw whole."
                )
                data["toto"] = [row for row in data["toto"] if row not in withheld]
                newest = max((r["draw_date"] for r in data["toto"]), default=None)
                toto_withheld = True

            # The prize table is only ever carried for the newest draw, and it
            # has to be carried for the newest draw. Older overlay rows drop
            # theirs: the app lays this file over its bundled archive, which
            # already holds the historical prize data, so keeping a second copy
            # here only grows the file every phone downloads.
            #
            # SUPERSEDED, NOT MERELY DELETED. The strip waits until the newest
            # draw actually carries its own table, because the numbers go up
            # before the table does and stripping on the numbers alone leaves
            # the app with no prize data at all — not for the draw that just
            # happened, and not for the one before it either. That is what
            # draw 4211 shipped as. With the withholding above the newest
            # published draw always has its table, but the guard stays: it is
            # what makes the two rules independent of each other.
            newest_has_table = any(
                r["draw_date"] == newest and r.get("shares") for r in data["toto"]
            )
            if newest_has_table:
                for row in data["toto"]:
                    if row["draw_date"] == newest:
                        continue
                    if "shares" in row or "group_1_prize" in row:
                        row.pop("shares", None)
                        row.pop("group_1_prize", None)

            # Said loudly, because it is the one thing on Latest Draw people
            # open the app for. Reaching this now means a draw that was already
            # published has lost its table, which should not be possible.
            if newest and not newest_has_table:
                print(f"TOTO: WARNING — newest draw {newest} has no prize table yet")

        before = (data.get("upcoming") or {}).get(key)
        after = upcoming_block(game, scraper, session, before)
        # The next-draw date moves the moment a draw is made: within seconds of
        # 6.30pm the page stops saying "Thu 27 Aug" and starts saying "Mon 31
        # Aug". Publishing that on its own, while the draw it steps over is
        # being withheld for its prize table, is the same partial update in a
        # different place — the app is told Thursday's draw has happened and
        # handed Monday's date, with Thursday's result nowhere in the file.
        # That is exactly what a user sees as "the next draw updated but the
        # result did not". It travels with the draw or not at all.
        if toto_withheld and game == "TOTO":
            print("TOTO: next-draw date held back with the draw it follows")
        elif meaningful(after) != meaningful(before):
            data.setdefault("upcoming", {})[key] = after

    data["version"] = SCHEMA_VERSION
    if payload(data) == published_before:
        print("nothing new — latest.json untouched")
        return False

    if args.dry_run:
        print("dry run — not writing")
        return False

    data["generated"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    # Written whole, then renamed, so a reader never sees a half-written file.
    tmp = LATEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(LATEST)
    print(f"latest.json updated — 4D {len(data['fourD'])} rows, TOTO {len(data['toto'])} rows")
    return True


# ─── the watch ───────────────────────────────────────────────────────────────
#
# WHY A LOOP AND NOT A SCHEDULE
# -----------------------------
# GitHub's `schedule:` is not a clock. Measured on this repo in the week to
# 4 Sep 2026: the workflow asked for twenty-two firings between 6.30pm and
# 10pm SGT every evening and was given NONE — not one, on any evening. The
# first run of each evening was the "10pm" entry, created around 10.40pm, and
# that run is what published Thursday's TOTO draw four hours after it was
# made. The phone's last look of the evening is 10.30pm, so the result was
# found by the next Doze maintenance window instead, at about 1am. Every part
# of that chain did its job; the schedule simply never started it.
#
# So the schedule is demoted to "start something, at some point in the
# afternoon", which it does manage — the noon entry has arrived between 1pm
# and 5.30pm every day — and the run itself owns the evening: it sleeps until
# 6.28pm, then looks every ninety seconds until both games are collected,
# committing and pushing each change the moment it lands. A 4D result goes out
# at 6.35 while the TOTO prize table is still being waited for. On a day with
# no draw the loop exits before opening a connection, exactly as before.
#
# A job may run for six hours. If this one cannot reach the end of the window
# it hands over: it sleeps out its budget and then dispatches a fresh run of
# the same workflow, which starts within a minute — `workflow_dispatch` is the
# one event GITHUB_TOKEN may raise that creates a run. The hand-over happens at
# most twice a day and only on a draw day with something still outstanding.
#
# COST TO SINGAPORE POOLS is two ~25KB fragments per look, on draw evenings
# only, in the gap between 6.28pm and the result — typically ten to twenty
# looks. A special draw whose result is late costs one look every four minutes
# until 11.30pm. Their own site's JavaScript polls the same two files.

WINDOW_OPEN = (18, 28)   # SGT, first look — results are never up before 6.30
WINDOW_CLOSE = (23, 30)  # SGT, last look — after this the catch-up runs own it
POLL_FAST_S = 90         # in the first hour after the draw, when it usually lands
POLL_SLOW_S = 240        # after that: a late prize table or a special draw
# Under GitHub's six-hour job limit, with room for the hand-over itself.
BUDGET = timedelta(hours=5, minutes=35)


def sgt_at(day: datetime, hm: tuple[int, int]) -> datetime:
    return day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def sleep_until(when: datetime, why: str) -> None:
    import time

    seconds = (when - datetime.now(SGT)).total_seconds()
    if seconds <= 0:
        return
    print(f"{why} — sleeping {int(seconds // 60)}m{int(seconds % 60):02d}s until {when:%H:%M} SGT", flush=True)
    time.sleep(seconds)


def publish() -> bool:
    """
    Commit and push latest.json, when running in the workflow.

    `publish.sh` is the same steps the workflow used to run once at the end of
    the job; with the job now living through the evening they run after every
    write instead, so the 4D result reaches phones while the TOTO prize table
    is still being waited for. Gated on HOBEH_PUBLISH so a `--watch` on a
    laptop never pushes anything.
    """
    import subprocess

    if os.environ.get("HOBEH_PUBLISH") != "1":
        print("HOBEH_PUBLISH is not set — written locally, not pushed")
        return True
    result = subprocess.run(["bash", str(HERE / "publish.sh")], check=False)
    if result.returncode != 0:
        print(f"publish.sh failed with {result.returncode} — will retry on the next change")
    return result.returncode == 0


def dispatch_successor() -> None:
    """
    Start another run of this workflow to carry on where this one's budget ends.

    Only from inside Actions, and only when `gh` can see a token. The workflow
    grants `actions: write` for exactly this call.
    """
    import subprocess

    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@")[0].split("/")[-1]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not (workflow and repo and os.environ.get("GH_TOKEN")):
        print("not in Actions, or no token — cannot dispatch a successor")
        return
    print(f"handing over: dispatching {workflow} on {repo}", flush=True)
    result = subprocess.run(
        ["gh", "workflow", "run", workflow, "--repo", repo, "--ref", "main"],
        check=False,
    )
    if result.returncode != 0:
        print("dispatch failed — the scheduled runs are the fallback")


def watch(args) -> int:
    started = datetime.now(SGT)
    budget_end = started + BUDGET
    print(f"watch started {started:%a %d %b %H:%M} SGT, budget until {budget_end:%H:%M}")

    while True:
        today = datetime.now(SGT).strftime("%Y-%m-%d")
        if not args.force and not due_games(read_latest(), today):
            print(f"nothing due on {today} — done", flush=True)
            return 0

        if run_pass(args):
            publish()
        if args.force:
            return 0
        if not due_games(read_latest(), today):
            print("everything due today has been published — done", flush=True)
            return 0

        now = datetime.now(SGT)
        open_at = sgt_at(now, WINDOW_OPEN)
        close_at = sgt_at(now, WINDOW_CLOSE)

        if now >= close_at:
            print("past the end of the window — the catch-up runs have it from here")
            return 0

        if now < open_at:
            # Before the draw. Wait for it — or, if this job cannot stay awake
            # long enough to see the result, wait out the budget and hand over
            # so the successor's six hours start as close to 6.30 as possible.
            if open_at + timedelta(minutes=45) > budget_end:
                sleep_until(budget_end - timedelta(minutes=2), "budget ends before the draw")
                dispatch_successor()
                return 0
            sleep_until(open_at, "waiting for the draw")
            continue

        poll = POLL_FAST_S if now < open_at + timedelta(hours=1) else POLL_SLOW_S
        if now + timedelta(seconds=poll) > budget_end:
            dispatch_successor()
            return 0
        sleep_until(now + timedelta(seconds=poll), f"still due: {', '.join(due_games(read_latest(), today))}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", choices=["4D", "TOTO", "all"], default="all")
    parser.add_argument("--check-scraper", action="store_true",
                        help="only verify the vendored scraper, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape and report, but do not write latest.json")
    parser.add_argument("--force", action="store_true",
                        help="scrape even when nothing is due today")
    parser.add_argument("--watch", action="store_true",
                        help="stay up through the evening and publish each result as it lands")
    args = parser.parse_args()

    if args.check_scraper:
        return 0 if check_scraper_matches() else 1
    if not check_scraper_matches():
        return 1

    if args.watch:
        return watch(args)
    run_pass(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
