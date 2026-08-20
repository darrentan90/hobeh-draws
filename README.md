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
