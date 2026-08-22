#!/usr/bin/env python3
"""One line naming the newest draw of each game, for the commit message.

Its own file rather than inline in the workflow: quoting multi-line Python
inside a YAML `run:` block inside a shell `$( )` is three levels of escaping,
and it breaks silently — the commit still lands, just with a mangled message.
"""

import json
from pathlib import Path

data = json.loads((Path(__file__).resolve().parent / "latest.json").read_text())
newest = lambda rows: max((r["draw_date"] for r in rows), default="?")
print(f"Draws to 4D {newest(data['fourD'])} / TOTO {newest(data['toto'])}")
