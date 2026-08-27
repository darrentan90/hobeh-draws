# hobeh-draws

Recent Singapore 4D and TOTO results, as one static JSON file, for the My HoBeh
app's pull-to-refresh to fetch.

`latest.json` holds roughly a year of draws — about 50KB. It is not the archive:
the app bundles the full history (5,609 4D draws back to 1986, 1,866 TOTO draws
back to 2008) and lays this file over the top, so this only has to cover what
has been drawn since the last app release.

## Updating

From the app repo:

```sh
npm run publish-draws:push
```

That regenerates the file from the source notes and pushes it here. New draws
reach phones on the next pull-to-refresh — no app release involved.

## Format

```json
{
  "version": 1,
  "generated": "2026-08-20T12:31:32.808Z",
  "fourD": [
    { "draw_no": 5524, "draw_date": "2026-08-19", "day": "Wednesday",
      "first": "4478", "second": "1440", "third": "2761",
      "starters": ["1203", "…"], "consolations": ["0909", "…"] }
  ],
  "toto": [
    { "draw_no": 4210, "draw_date": "2026-08-20", "day": "Thursday",
      "numbers": [3, 18, 19, 25, 27, 48], "additional": 40 }
  ]
}
```

Both lists are newest-first. Dates are ISO. `draw_no` is `null` for early 4D
draws whose number was never recorded.

The **newest** TOTO draw also carries `shares` (the seven prize groups) and
`group_1_prize` (the pool as printed, which is not `winners × amount` and cannot
be recomputed from the table). No older row does: the app bundles the historical
prize data already, so a second copy here would only grow the file every phone
downloads. The old table is superseded when the new one lands, not before —
Singapore Pools puts the winning numbers up several minutes ahead of the prize
table, and dropping the previous draw's on the strength of the numbers alone
leaves the app with no prize data at all.

### A TOTO draw is published whole, or not at all

`shares` is not optional on a new TOTO row, and this is the rule that matters
most in the whole repo. Singapore Pools publishes the winning numbers several
minutes before the prize table, and this job used to forward each half as it
arrived: one commit with the numbers, another some minutes later with the
table. The app takes the newest row as *the* latest draw the instant it lands —
it opens Latest Draw on it and sends "the TOTO results are out" to every phone
— so the first of those two commits pushed a notification about a result whose
payouts did not exist yet, and whoever tapped it got half a screen.

So a newly collected TOTO draw whose prize table has not arrived is **withheld**
from `latest.json` entirely. `due_games` then sees no row for today, keeps TOTO
due, and a later run collects the numbers and the table together and publishes
them as one update. The `upcoming.toto` block is held back with it, because the
next-draw date moves the moment a draw is made — publishing "next draw: Monday"
while Thursday's result is being withheld is the same partial update wearing a
different hat, and it is exactly what a user sees as "the next draw updated but
the result never did".

The cost is re-fetching one result page on each run in the gap. That is only
the evening of a TOTO draw, and only the minutes between the two halves.

The app ignores a file whose `version` it does not recognise, so bumping that
number takes every older build off the feed. Add fields rather than changing
them where you can; unknown fields are dropped harmlessly.

Draw results are published by Singapore Pools.

## How this file gets updated

`.github/workflows/update-draws.yml` runs every ten minutes between 6.30pm and
10pm SGT, plus four catch-up runs spread through the following twelve hours, and
commits `latest.json` itself. **Nothing of mine has to be switched on** — not the MacBook, not the
pieface. The app reads this file straight from `raw.githubusercontent.com`.

    build_latest.py   decides what to fetch, converts it to the app's schema
    scraper.py        a byte-for-byte copy of the pieface updater's parser
    scraper.sha256    fails the build if that copy drifts
    summarise.py      the commit message

### GitHub's scheduler drops runs, and the catch-ups are the answer

Do not trust the evening window on its own. `schedule:` is best-effort, and
best-effort here does not mean "a few minutes late" — it means runs are never
created at all. Measured on this repo: the old `*/5` schedule asked for about
forty firings across the window and got **six** on 25 Aug 2026, **six** on
26 Aug, and **none whatever** on 27 Aug, a day a TOTO draw was made. Nothing was
disabled and nothing failed; GitHub simply never fired it, and the result the
scraper could see never reached a phone.

Hence two changes. The evening polls dropped to every ten minutes, because dense
schedules are the first ones shed and five minutes was buying latency that was
never delivered. And, more importantly, the day no longer ends at 10pm: there
are catch-up runs at midnight, 4am, 9am and noon SGT. Every evening run can be
dropped and the draw still gets published — by morning at the very worst — and
the catch-ups are free on any day the evening worked, because `due_games` exits
without opening a connection.

If a result is missing and you do not want to wait: **Actions → Update draws →
Run workflow**. With the scheduler behaving this way that is a normal tool, not
an emergency one.

### Why polling this often is not rude

Every run starts by asking whether a draw is even possible today, from
`upcoming.drawDate` — which Singapore Pools published, so it accounts for the
special and cascade draws the Wed/Sat/Sun and Mon/Thu pattern misses. Once a
game's draw for the day is collected, the rest of the evening's runs exit in
about a tenth of a second having opened no connection at all. On a night with
no draw, that is every one of them.

A TOTO draw is not "collected" until its prize table has arrived as well as its
numbers, because the two are published minutes apart and the table is the half
people open the app for. So a TOTO evening keeps checking through that gap, at
one fragment per run and no result pages.

So the traffic is roughly: one cheap fragment fetch every ten minutes while
genuinely waiting for a result, one result page per run through the gap between
a TOTO draw's numbers and its prize table, then nothing at all. Expect "within
ten to thirty minutes" of a result being published, and read the section above
before assuming a longer gap is a bug in this repo.

The pieface updater still writes the Obsidian vault on its own timers. That is
now an **independent** job — it can be off for a week without the app noticing,
and the app being current says nothing about whether the vault is.

`npm run publish-draws:push` in the app repo **no longer exists** — `--push` is
refused there. The vault is a historical record now and can be behind, so
publishing from it would walk every phone backwards. Use `workflow_dispatch`
above instead.

### Keeping the two parsers in step

`scraper.py` is copied, not imported, because the pieface and GitHub cannot
share a filesystem. After editing the pieface copy:

    cp ~/AIOS-Vault/Efforts/买HoBeh/pieface-updater/update_4d_toto.py scraper.py
    shasum -a 256 scraper.py | awk '{print $1"  scraper.py"}' > scraper.sha256

The workflow's first step is `--check-scraper`, so a forgotten copy fails the
run loudly rather than publishing whatever the stale parser produces.
