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
draws whose number was never recorded. TOTO carries no prize/share data here —
the app keeps its own for the draws it has.

The app ignores a file whose `version` it does not recognise, so bumping that
number takes every older build off the feed. Add fields rather than changing
them where you can; unknown fields are dropped harmlessly.

Draw results are published by Singapore Pools.

## How this file gets updated

`.github/workflows/update-draws.yml` runs on a schedule and commits `latest.json`
itself. **Nothing of mine has to be switched on** — not the MacBook, not the
pieface. The app reads this file straight from `raw.githubusercontent.com`.

    build_latest.py   decides what to fetch, converts it to the app's schema
    scraper.py        a byte-for-byte copy of the pieface updater's parser
    scraper.sha256    fails the build if that copy drifts
    summarise.py      the commit message

The pieface updater still writes the Obsidian vault on its own timers. That is
now an **independent** job — it can be off for a week without the app noticing,
and the app being current says nothing about whether the vault is.

`npm run publish-draws:push` in the app repo still works and still publishes
from the vault. It is now a manual override for when this workflow is broken,
not the normal path — and it will overwrite whatever the workflow last wrote.

### Keeping the two parsers in step

`scraper.py` is copied, not imported, because the pieface and GitHub cannot
share a filesystem. After editing the pieface copy:

    cp ~/AIOS-Vault/Efforts/买HoBeh/pieface-updater/update_4d_toto.py scraper.py
    shasum -a 256 scraper.py | awk '{print $1"  scraper.py"}' > scraper.sha256

The workflow's first step is `--check-scraper`, so a forgotten copy fails the
run loudly rather than publishing whatever the stale parser produces.
