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
    """
    due = []
    for game, key in (("4D", "fourD"), ("TOTO", "toto")):
        if any(row["draw_date"] == today for row in data.get(key) or []):
            continue  # already collected — done for the day
        expected = ((data.get("upcoming") or {}).get(key) or {}).get("drawDate")
        if not expected or expected <= today:
            due.append(game)
    return due


def collect(game: str, existing: list, scraper, session) -> list:
    """Fetch draws newer than what is already published."""
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
        return []

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
    return added


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", choices=["4D", "TOTO", "all"], default="all")
    parser.add_argument("--check-scraper", action="store_true",
                        help="only verify the vendored scraper, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape and report, but do not write latest.json")
    parser.add_argument("--force", action="store_true",
                        help="scrape even when nothing is due today")
    args = parser.parse_args()

    if args.check_scraper:
        return 0 if check_scraper_matches() else 1
    if not check_scraper_matches():
        return 1

    data = read_latest()
    today = datetime.now(SGT).strftime("%Y-%m-%d")
    games = ["4D", "TOTO"] if args.game == "all" else [args.game]

    # Checked before the scraper is even imported, let alone a socket opened.
    if not args.force:
        games = [g for g in games if g in due_games(data, today)]
        if not games:
            print(f"nothing due on {today} — no request made")
            return 0
    print(f"due today ({today}): {', '.join(games)}")

    scraper = load_scraper()
    session = requests.Session()

    changed = False
    for game in games:
        key = "fourD" if game == "4D" else "toto"
        added = collect(game, data[key], scraper, session)
        if added:
            data[key] = trim(data[key] + added)
            changed = True

        # TOTO prize tables live only in the snapshot, and only for the newest
        # draw. Attached after the merge so it finds the row wherever it landed.
        if game == "TOTO":
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
                    if row.get("shares") != shares:
                        row["shares"] = shares
                        changed = True
                    if pool is not None and row.get("group_1_prize") != int(pool):
                        row["group_1_prize"] = int(pool)
                        changed = True
                    break

            # The prize table is only ever carried for the newest draw, and it
            # has to be carried for the newest draw. Older overlay rows drop
            # theirs: the app lays this file over its bundled archive, which
            # already holds the historical prize data, so keeping a second copy
            # here only grows the file every phone downloads.
            for row in data["toto"]:
                if row["draw_date"] == newest:
                    continue
                if "shares" in row or "group_1_prize" in row:
                    row.pop("shares", None)
                    row.pop("group_1_prize", None)
                    changed = True

            # Said loudly, because it is the one thing on Latest Draw people
            # open the app for. The snapshot sometimes publishes the numbers
            # before the prize table, so a later run in the evening picks it up.
            if newest and not any(
                r["draw_date"] == newest and r.get("shares") for r in data["toto"]
            ):
                print(f"TOTO: WARNING — newest draw {newest} has no prize table yet")

        before = (data.get("upcoming") or {}).get(key)
        after = upcoming_block(game, scraper, session, before)
        if meaningful(after) != meaningful(before):
            data.setdefault("upcoming", {})[key] = after
            changed = True

    if not changed:
        print("nothing new — latest.json untouched")
        return 0

    if args.dry_run:
        print("dry run — not writing")
        return 0

    data["version"] = SCHEMA_VERSION
    data["generated"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    # Written whole, then renamed, so a reader never sees a half-written file.
    tmp = LATEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(LATEST)
    print(f"latest.json updated — 4D {len(data['fourD'])} rows, TOTO {len(data['toto'])} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
