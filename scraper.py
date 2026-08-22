#!/usr/bin/env python3
"""
买HoBeh 4D + TOTO results updater  —  year-agnostic, unattended.

Keeps these files current:
    <HOBEH_DIR>/4d_results_json/4d_results_<YYYY>.md
    <HOBEH_DIR>/toto_results_json/toto_results_<YYYY>.md

How it decides what to fetch:
  * Reads EVERY year file for a game, finds the highest draw_no already saved,
    and fetches forward from there — one draw number at a time.
  * Each fetched draw is filed into the year file matching its OWN draw date,
    so a draw on Fri 01 Jan 2027 lands in 4d_results_2027.md automatically.
  * If that year's file doesn't exist yet, it is created with the same
    frontmatter + heading + JSON-array shape the existing files use, so the
    Obsidian dataviewjs views in "Past 4D Results.md" / "Past TOTO Results.md"
    pick it up with no changes.
  * Singapore Pools silently serves the latest real draw when you ask for a
    draw number that doesn't exist yet — that's how we detect "caught up".

Safe to run as often as you like: when there is nothing new it touches no
files at all. A lock file prevents overlapping runs.

Config (environment, or hobeh.env next to this script):
    HOBEH_DIR   path to the 买HoBeh folder            (required)
    HOBEH_LOG   log file path                         (default: <script dir>/update.log)
    HOBEH_MAX   max new draws per game per run        (default: 30)

Usage:
    python3 update_4d_toto.py            # normal run
    python3 update_4d_toto.py --dry-run  # scrape + report, write nothing
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_env_file() -> None:
    """Load KEY=value lines from hobeh.env beside this script (env wins)."""
    env_file = SCRIPT_DIR / "hobeh.env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file()

HOBEH_DIR = Path(os.environ.get("HOBEH_DIR", "")).expanduser()
LOG_PATH = Path(os.environ.get("HOBEH_LOG", SCRIPT_DIR / "update.log")).expanduser()
MAX_NEW_PER_GAME = int(os.environ.get("HOBEH_MAX", "30"))

FOUR_D_URL = "https://www.singaporepools.com.sg/en/product/Pages/4d_results.aspx?sppl="
TOTO_URL = "https://www.singaporepools.com.sg/en/product/Pages/toto_results.aspx?sppl="

# Snapshot sources — the next jackpot / next draw / winning-shares breakdown,
# which the per-draw-number pages above don't carry.
#
# NOT the results pages (…/Pages/toto_results.aspx). Those are a JavaScript
# shell: fetched with requests they contain only the template, with dummy
# values like "01 02 03 04 05 06" and "$123,343" where the real numbers go, and
# no "Draw No. NNNN" text for find_latest_draw_container() to anchor on. That
# is why the snapshot scrape reported "could not locate latest-draw container"
# on every run while the history scrape above kept working.
#
# What the page's own JavaScript does is fetch these pre-generated fragments
# and inject them, so going straight to them gets the same HTML without a
# browser. Each game needs two: one for the latest result, one for what is
# coming next. They are concatenated and handed to the existing parsers, which
# work on them unchanged.
SNAPSHOT_BASE = "https://www.singaporepools.com.sg/DataFileArchive/Lottery/Output/"
SNAPSHOT_FRAGMENTS = {
    "4D": ("fourd_next_draw_info_en.html", "fourd_result_top_draws_en.html"),
    "TOTO": ("toto_next_draw_estimate_en.html", "toto_result_top_draws_en.html"),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

DELAY = 1.5
MAX_RETRIES = 3

log = logging.getLogger("hobeh")


def setup_logging(verbose: bool = True) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:  # log file unwritable — carry on with stdout only
        print(f"warning: cannot write log file {LOG_PATH}: {exc}", file=sys.stderr)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


# --------------------------------------------------------------------------
# Single-instance lock
# --------------------------------------------------------------------------

class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        import fcntl
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise SystemExit("another run is already in progress — exiting")
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        import fcntl
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


# --------------------------------------------------------------------------
# Game definitions
# --------------------------------------------------------------------------

GAMES = {
    "4D": {
        "subdir": "4d_results_json",
        "prefix": "4d_results_",
        "title": "4D Results {year}",
        "url": FOUR_D_URL,
    },
    "TOTO": {
        "subdir": "toto_results_json",
        "prefix": "toto_results_",
        "title": "TOTO Results {year}",
        "url": TOTO_URL,
    },
}


def year_file(game: str, year: int) -> Path:
    g = GAMES[game]
    return HOBEH_DIR / g["subdir"] / f"{g['prefix']}{year}.md"


def new_file_header(game: str, year: int) -> str:
    return f"---\nyear: {year}\n---\n\n# {GAMES[game]['title'].format(year=year)}\n\n"


# --------------------------------------------------------------------------
# Reading / writing the .md files
# --------------------------------------------------------------------------

ARRAY_RE = re.compile(r"\[\s*\n(.*)\n\]\s*$", re.S)


def load_file(path: Path):
    """Return (header, entries). Missing/empty array files are handled."""
    if not path.exists():
        return None, []
    text = path.read_text(encoding="utf-8")
    m = ARRAY_RE.search(text)
    if not m:
        # File exists but has no populated array yet (e.g. freshly stubbed).
        stub = re.search(r"\[\s*\]\s*$", text)
        if stub:
            return text[: stub.start()], []
        raise ValueError(f"Could not find a JSON array in {path}")
    header = text[: m.start()]
    entries = json.loads("[" + m.group(1) + "]")
    return header, entries


def save_file(path: Path, header: str, entries: list) -> None:
    """Atomic write: temp file in the same dir, then rename."""
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    new_text = header + "[\n" + ",\n".join(lines) + "\n]\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)


def scan_game(game: str):
    """Return (files_by_year, max_draw_no, latest_date_str)."""
    g = GAMES[game]
    folder = HOBEH_DIR / g["subdir"]
    if not folder.is_dir():
        raise SystemExit(f"folder not found: {folder}  (check HOBEH_DIR)")

    files_by_year: dict[int, dict] = {}
    max_draw = 0
    latest_date = None

    pattern = re.compile(rf"^{re.escape(g['prefix'])}(\d{{4}})\.md$")
    for path in sorted(folder.iterdir()):
        m = pattern.match(path.name)
        if not m:
            continue
        year = int(m.group(1))
        header, entries = load_file(path)
        files_by_year[year] = {
            "path": path,
            "header": header if header is not None else new_file_header(game, year),
            "entries": entries,
            "dirty": False,
        }
        for e in entries:
            try:
                dn = int(e["draw_no"])
            except (KeyError, ValueError, TypeError):
                continue
            if dn > max_draw:
                max_draw = dn
                latest_date = e.get("draw_date")

    if not files_by_year:
        raise SystemExit(f"no {g['prefix']}YYYY.md files found in {folder}")

    return files_by_year, max_draw, latest_date


def bucket_for(game: str, files_by_year: dict, year: int) -> dict:
    """Get (creating if needed) the in-memory bucket for a year."""
    if year not in files_by_year:
        path = year_file(game, year)
        files_by_year[year] = {
            "path": path,
            "header": new_file_header(game, year),
            "entries": [],
            "dirty": False,
        }
        log.info("  new year detected — will create %s", path.name)
    return files_by_year[year]


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def encode_draw(draw_no: int) -> str:
    return base64.b64encode(f"DrawNumber={draw_no}".encode()).decode()


def parse_date(text: str):
    text = text.strip()
    for fmt in ("%a, %d %b %Y", "%A, %d %b %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fetch_page(url: str, session: requests.Session, label: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)
        except Exception as exc:
            log.warning("  [%s] attempt %d failed: %s", label, attempt, exc)
            time.sleep(2 * attempt)
    return None


DATE_RE = re.compile(
    r"(Sun|Mon|Tue|Wed|Thu|Fri|Sat),?\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", re.I
)
DRAW_NO_RE = re.compile(r"Draw\s*No\.?\s*(\d+)", re.I)


def common_head(text: str, draw_no: int):
    """Shared date / draw-number parsing. Returns (draw_date, parsed_draw) or a
    {'not_found': ...} marker, or None if unparseable."""
    dm = DATE_RE.search(text)
    if not dm:
        return None
    draw_date = parse_date(dm.group(0))
    if not draw_date:
        return None
    nm = DRAW_NO_RE.search(text)
    parsed = int(nm.group(1)) if nm else draw_no
    if parsed != draw_no:
        return {"not_found": True, "latest_available": parsed}
    return draw_date, parsed


def scrape_4d_draw(draw_no: int, session: requests.Session):
    text = fetch_page(FOUR_D_URL + encode_draw(draw_no), session, f"4D {draw_no}")
    if text is None:
        return None
    head = common_head(text, draw_no)
    if head is None or isinstance(head, dict):
        return head
    draw_date, parsed_draw = head

    def find_after(label):
        m = re.search(rf"{label}\s*[:\-]?\s*(\d{{4}})", text, re.I)
        return m.group(1) if m else None

    first = find_after(r"1st\s*Prize")
    second = find_after(r"2nd\s*Prize")
    third = find_after(r"3rd\s*Prize")
    if not all([first, second, third]):
        return None

    all_nums = re.findall(r"\b(\d{4})\b", text)
    used = {first, second, third, str(parsed_draw), str(draw_date.year)}
    remaining = [n for n in all_nums if n not in used]
    starters, consolations = remaining[:10], remaining[10:20]
    if len(starters) != 10 or len(consolations) != 10:
        log.warning("  4D %d: unexpected prize counts (%d starters, %d consolations)",
                    draw_no, len(starters), len(consolations))
        return None

    return {
        "draw_date": draw_date.strftime("%a, %d %b %Y"),
        "draw_no": str(parsed_draw),
        "first": first,
        "second": second,
        "third": third,
        "starters": starters,
        "consolations": consolations,
    }


def scrape_toto_draw(draw_no: int, session: requests.Session):
    text = fetch_page(TOTO_URL + encode_draw(draw_no), session, f"TOTO {draw_no}")
    if text is None:
        return None
    head = common_head(text, draw_no)
    if head is None or isinstance(head, dict):
        return head
    draw_date, parsed_draw = head

    win = re.search(
        r"Winning\s*Numbers?\s*[:\-]?\s*"
        r"(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})",
        text, re.I,
    )
    add = re.search(r"Additional\s*Number\s*[:\-]?\s*(\d{1,2})", text, re.I)

    if win and add:
        numbers = sorted(int(win.group(i)) for i in range(1, 7))
        additional = int(add.group(1))
    else:
        candidates = [int(n) for n in re.findall(r"\b(\d{1,2})\b", text)
                      if 1 <= int(n) <= 49]
        if len(candidates) < 7:
            return None
        numbers = sorted(candidates[:6])
        additional = candidates[6]

    if len(set(numbers)) != 6 or not all(1 <= n <= 49 for n in numbers) \
            or not 1 <= additional <= 49:
        log.warning("  TOTO %d: numbers failed sanity check: %s + %s",
                    draw_no, numbers, additional)
        return None

    return {
        "draw_date": draw_date.strftime("%a, %d %b %Y"),
        "draw_no": str(parsed_draw),
        "numbers": numbers,
        "additional": additional,
    }


SCRAPERS = {"4D": scrape_4d_draw, "TOTO": scrape_toto_draw}


# --------------------------------------------------------------------------
# "Latest draw" snapshot — jackpot, next draw, winning shares.
#
# Separate from the per-draw history above: always overwritten with just the
# current state (not appended), and NOT written into the yearly *_results_*.md
# files. Lives at:
#     <HOBEH_DIR>/toto_results_json/toto_latest.json
#     <HOBEH_DIR>/4d_results_json/4d_latest.json
# so an app can always read "the latest draw" from one fixed path.
# --------------------------------------------------------------------------

NEXT_DRAW_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+\w+\s+\d{4}\s*,?\s*\d{1,2}\.\d{2}\s*[ap]m",
    re.I,
)


def parse_ampm_time(s: str):
    """'6.30pm' -> '18:30'. None if unparseable."""
    m = re.match(r"(\d{1,2})\.(\d{2})\s*([ap]m)", s.strip(), re.I)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def parse_next_draw_field(raw: str | None):
    """Turn 'Thu, 20 Aug 2026 , 6.30pm' into a small structured dict, or None."""
    if not raw:
        return None
    date_match = DATE_RE.search(raw)
    time_match = re.search(r"\d{1,2}\.\d{2}\s*[ap]m", raw, re.I)
    date_obj = parse_date(date_match.group(0)) if date_match else None
    return {
        "raw": raw.strip(),
        "date": date_obj.strftime("%Y-%m-%d") if date_obj else None,
        "time_24h": parse_ampm_time(time_match.group(0)) if time_match else None,
    }


def fetch_snapshot_soup(game: str, session: requests.Session):
    """Fetch this game's pre-generated fragments and parse them as one document.

    A cache-busting query string is appended for the same reason the site's own
    JavaScript appends one: these are static files behind a CDN, and the whole
    point of a snapshot is that it is current. Every fragment must arrive — a
    half-fetched snapshot would parse as a successful one missing exactly the
    field that failed.
    """
    bust = int(time.time())
    parts = []
    for name in SNAPSHOT_FRAGMENTS[game]:
        url = f"{SNAPSHOT_BASE}{name}?_={bust}"
        html = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = session.get(url, headers=HEADERS, timeout=25)
                resp.raise_for_status()
                html = resp.text
                break
            except Exception as exc:
                log.warning("  [%s snapshot] %s attempt %d failed: %s",
                            game, name, attempt, exc)
                time.sleep(2 * attempt)
        if html is None:
            log.warning("  [%s snapshot] giving up on %s", game, name)
            return None
        parts.append(html)
    return BeautifulSoup("\n".join(parts), "html.parser")


def find_latest_draw_container(soup: BeautifulSoup):
    """Same technique as the original sg_pools_scraper.py: find the 'Draw
    No. NNNN' text node and walk up to a parent with enough text to hold the
    whole result block."""
    draw_sections = soup.find_all(string=re.compile(r"Draw No\.\s*\d+"))
    if not draw_sections:
        return None
    container = draw_sections[0].find_parent()
    while container and len(container.get_text()) < 100:
        container = container.find_parent()
    return container


def parse_toto_snapshot(soup: BeautifulSoup):
    """Pure parsing (no network) — kept separate from the fetch so it can be
    unit-tested against a static HTML fixture."""
    page_text = soup.get_text(" ", strip=True)

    jackpot_match = re.search(r"\$[\d,]+(?:\.\d+)?\s*est", page_text, re.I)
    next_draw_match = NEXT_DRAW_RE.search(page_text)

    container = find_latest_draw_container(soup)
    if container is None:
        log.warning("  TOTO snapshot: could not locate latest-draw container")
        return None
    cont_text = container.get_text(" ", strip=True)

    date_match = re.search(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+\w+\s+\d{4}\s+Draw No\.\s*(\d+)",
        cont_text, re.I,
    )
    if not date_match:
        log.warning("  TOTO snapshot: could not parse latest draw date/no")
        return None
    draw_date_raw = date_match.group(0).split("Draw No")[0].strip()
    draw_no = date_match.group(2)

    numbers_match = re.search(
        r"Winning Numbers\s+([\d\s]+)\s+Additional Number\s+(\d+)", cont_text, re.I,
    )
    if not numbers_match:
        log.warning("  TOTO snapshot: could not parse winning numbers")
        return None
    numbers = [int(n) for n in numbers_match.group(1).strip().split()]
    additional = int(numbers_match.group(2))
    if len(numbers) != 6 or len(set(numbers)) != 6 or not all(1 <= n <= 49 for n in numbers) \
            or not 1 <= additional <= 49:
        log.warning("  TOTO snapshot: numbers failed sanity check: %s + %s",
                    numbers, additional)
        return None

    g1_match = re.search(r"Group 1 Prize\s+(\$[\d,]+(?:\.\d+)?|-)", cont_text, re.I)
    group_1_prize = g1_match.group(1) if g1_match else None

    # The share COUNT needs [\d,] just as the amount does. With a bare (\d+)
    # the thousands separator ends the match, so "Group 5 $50 8,028" was read
    # as 8 winners — the groups that matter most here are exactly the ones with
    # five- and six-figure counts, so the error was invisible in Groups 2-4 and
    # wrong by three orders of magnitude in Groups 5-7.
    shares = re.findall(
        r"Group\s+(\d+)\s+(\$[\d,]+(?:\.\d+)?|-)\s+([\d,]+|-)", cont_text, re.I,
    )
    winning_shares = [
        {"prize_group": f"Group {g}", "share_amount": amt, "no_of_winning_shares": cnt}
        for g, amt, cnt in shares
    ]
    if not winning_shares:
        log.warning("  TOTO snapshot: no winning-shares rows found")

    return {
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "next_jackpot": jackpot_match.group(0).strip() if jackpot_match else None,
        "next_draw": parse_next_draw_field(next_draw_match.group(0) if next_draw_match else None),
        "latest_draw": {
            "draw_date": draw_date_raw,
            "draw_no": draw_no,
            "numbers": numbers,
            "additional": additional,
            "group_1_prize": group_1_prize,
        },
        "winning_shares": winning_shares,
    }


def parse_4d_snapshot(soup: BeautifulSoup):
    """4D has no jackpot / pooled winning-shares concept — fixed payout per
    prize tier — so this only carries the latest draw + next draw date."""
    page_text = soup.get_text(" ", strip=True)

    next_draw_match = NEXT_DRAW_RE.search(page_text)

    date_match = re.search(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+\w+\s+\d{4}\s+Draw No\.\s*(\d+)",
        page_text, re.I,
    )
    if not date_match:
        log.warning("  4D snapshot: could not parse latest draw date/no")
        return None
    draw_date_raw = date_match.group(0).split("Draw No")[0].strip()
    draw_no = date_match.group(2)

    def find_after(label):
        m = re.search(rf"{label}\s+Prize\s+(\d{{4}})", page_text, re.I)
        return m.group(1) if m else None

    first = find_after("1st")
    second = find_after("2nd")
    third = find_after("3rd")

    starter_section = re.search(r"Starter Prizes\s+((?:\d{4}\s*){10})", page_text, re.I)
    consol_section = re.search(r"Consolation Prizes\s+((?:\d{4}\s*){10})", page_text, re.I)
    starters = starter_section.group(1).split() if starter_section else []
    consolations = consol_section.group(1).split() if consol_section else []

    if not all([first, second, third]) or len(starters) != 10 or len(consolations) != 10:
        log.warning(
            "  4D snapshot: incomplete parse (1st=%s 2nd=%s 3rd=%s starters=%d consolations=%d)",
            first, second, third, len(starters), len(consolations),
        )
        return None

    return {
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "next_draw": parse_next_draw_field(next_draw_match.group(0) if next_draw_match else None),
        "latest_draw": {
            "draw_date": draw_date_raw,
            "draw_no": draw_no,
            "first": first,
            "second": second,
            "third": third,
            "starters": starters,
            "consolations": consolations,
        },
    }


def scrape_toto_snapshot(session: requests.Session):
    soup = fetch_snapshot_soup("TOTO", session)
    return parse_toto_snapshot(soup) if soup is not None else None


def scrape_4d_snapshot(session: requests.Session):
    soup = fetch_snapshot_soup("4D", session)
    return parse_4d_snapshot(soup) if soup is not None else None


SNAPSHOT_SCRAPERS = {"4D": scrape_4d_snapshot, "TOTO": scrape_toto_snapshot}


def snapshot_file(game: str) -> Path:
    g = GAMES[game]
    name = "4d_latest.json" if game == "4D" else "toto_latest.json"
    return HOBEH_DIR / g["subdir"] / name


def save_json(path: Path, data: dict) -> None:
    """Atomic write, same pattern as save_file()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def update_snapshot(game: str, dry_run: bool = False) -> bool:
    session = requests.Session()
    data = SNAPSHOT_SCRAPERS[game](session)
    path = snapshot_file(game)

    if data is None:
        log.warning("  %s snapshot: scrape failed — leaving %s untouched",
                    game, path.name)
        return False

    if dry_run:
        log.info("  DRY RUN — would write %s (draw %s)",
                 path.name, data["latest_draw"]["draw_no"])
        return True

    save_json(path, data)
    log.info("  saved %s (draw %s)", path.name, data["latest_draw"]["draw_no"])
    return True


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def update_game(game: str, dry_run: bool = False) -> int:
    log.info("=== %s ===", game)
    files_by_year, last_draw, last_date = scan_game(game)
    log.info("  last saved draw: %s (%s) across %d year file(s)",
             last_draw, last_date, len(files_by_year))

    session = requests.Session()
    scraper = SCRAPERS[game]
    added = 0
    draw_no = last_draw + 1

    while added < MAX_NEW_PER_GAME:
        result = scraper(draw_no, session)

        if result is None:
            log.warning("  draw %d: could not parse — stopping (will retry next run)",
                        draw_no)
            break

        if result.get("not_found"):
            log.info("  draw %d not published yet (latest available: %s) — up to date",
                     draw_no, result["latest_available"])
            break

        year = datetime.strptime(result["draw_date"], "%a, %d %b %Y").year
        bucket = bucket_for(game, files_by_year, year)
        if any(e.get("draw_no") == result["draw_no"] for e in bucket["entries"]):
            log.info("  draw %s already present — skipping", result["draw_no"])
        else:
            bucket["entries"].append(result)
            bucket["dirty"] = True
            added += 1
            log.info("  + draw %s  %s  -> %s",
                     result["draw_no"], result["draw_date"], bucket["path"].name)

        draw_no += 1
        time.sleep(DELAY)
    else:
        log.warning("  hit the %d-draw cap for this run — run again to continue",
                    MAX_NEW_PER_GAME)

    if not added:
        log.info("  nothing new — no files touched")
        return 0

    if dry_run:
        log.info("  DRY RUN — %d draw(s) would be written, nothing saved", added)
        return added

    for year in sorted(files_by_year):
        b = files_by_year[year]
        if not b["dirty"]:
            continue
        b["entries"].sort(key=lambda e: int(e["draw_no"]))
        save_file(b["path"], b["header"], b["entries"])
        log.info("  saved %s (%d entries)", b["path"].name, len(b["entries"]))

    return added


def main() -> int:
    ap = argparse.ArgumentParser(description="Update 买HoBeh 4D + TOTO result files")
    ap.add_argument("--dry-run", action="store_true",
                    help="scrape and report, but write nothing")
    ap.add_argument("--quiet", action="store_true", help="log to file only")
    ap.add_argument("--game", choices=["4D", "TOTO"],
                    help="update only one game (default: both)")
    args = ap.parse_args()

    setup_logging(verbose=not args.quiet)

    if not str(HOBEH_DIR):
        log.error("HOBEH_DIR is not set. Set it in hobeh.env or the environment.")
        return 2
    if not HOBEH_DIR.is_dir():
        log.error("HOBEH_DIR does not exist: %s", HOBEH_DIR)
        return 2

    log.info("run start — vault: %s", HOBEH_DIR)
    total = 0
    failed = []
    for game in ([args.game] if args.game else ["4D", "TOTO"]):
        try:
            total += update_game(game, dry_run=args.dry_run)
        except SystemExit:
            raise
        except Exception as exc:
            log.exception("  %s failed: %s", game, exc)
            failed.append(game)

        try:
            if not update_snapshot(game, dry_run=args.dry_run):
                failed.append(f"{game} snapshot")
        except Exception as exc:
            log.exception("  %s snapshot failed: %s", game, exc)
            failed.append(f"{game} snapshot")

    log.info("run end — %d new draw(s) total%s\n",
             total, f", FAILED: {', '.join(failed)}" if failed else "")
    return 1 if failed else 0


if __name__ == "__main__":
    lock = SCRIPT_DIR / ".update.lock"
    with SingleInstance(lock):
        sys.exit(main())
