#!/usr/bin/env python3
"""Does the MLBAM name search tell two same-named players apart?

⚠️ WHY THIS EXISTS. `_resolve_mlbam_id_by_name` accepts a multi-hit search
only when exactly ONE result matches the queried name. That comparison used a
raw `.lower()`, and BDL ships names unaccented while MLB spells them properly.
So for "Hector Rodriguez", MLB's "Héctor Rodríguez" (b.2004, the actual Reds
outfielder) did NOT count as a match, leaving his 1920 Negro Leagues namesake
as the unique "exact" one. The nightly then wrote the young man's bio and a
2026 season onto the old man's row — every night, undoing the repair each
morning, until the accents were normalised.

The neighbouring case is the control: "Eliezer Alfonzo" is unaccented in both
spellings, so BOTH matched, the search was ambiguous, and nothing was written.
Same night, same code, opposite outcomes — the diacritic was the difference.

AST-extracted with a stubbed HTTP layer, so this runs offline.
Run: python3 backend/tests/test_mlbam_name_resolve.py
"""

import ast
import io
import json
import os
import sys
import typing
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "api", "data_service.py")

# MLB Stats API responses, as the live service returns them.
PEOPLE = {
    "Hector Rodriguez": [
        {"id": 699302, "fullName": "Héctor Rodríguez"},   # b.2004, the real one
        {"id": 121356, "fullName": "Hector Rodriguez"},   # b.1920, the namesake
    ],
    "Eliezer Alfonzo": [
        {"id": 672613, "fullName": "Eliezer Alfonzo"},    # b.1999
        {"id": 425769, "fullName": "Eliezer Alfonzo"},    # b.1979
    ],
    "Bobby Miller":  [{"id": 676272, "fullName": "Bobby Miller"}],
    "Hogan Harris":  [{"id": 663687, "fullName": "Hogan Harris"}],
    "Nobody At All": [],
}


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _build():
    """Extract the real function with a stubbed urllib and logger."""
    import re
    import urllib.parse as _up

    class _FakeReq:
        def __init__(self, url, headers=None): self.url = url

    def _fake_urlopen(req, timeout=None):
        url = req.url if hasattr(req, "url") else str(req)
        qs = _up.parse_qs(url.split("?", 1)[1])
        name = qs.get("names", [""])[0]
        return _Resp(json.dumps({"people": PEOPLE.get(name, [])}).encode())

    fake_urllib = type("U", (), {
        "parse":   _up,
        "request": type("R", (), {"Request": _FakeReq, "urlopen": staticmethod(_fake_urlopen)}),
    })
    ns = {
        "Optional": typing.Optional, "unicodedata": unicodedata, "re": re,
        "json": json, "urllib": fake_urllib,
        "log": type("L", (), {"warning": staticmethod(lambda *a, **k: None)})(),
    }
    tree = ast.parse(open(SRC).read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<ds>", "exec"), ns)
            except Exception:
                pass
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_normalize_player_name", "_to_int", "_resolve_mlbam_id_by_name",
        ):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<ds>", "exec"), ns)
    return ns["_resolve_mlbam_id_by_name"]


def main():
    resolve = _build()
    bad = []

    def check(name, want, why):
        got = resolve(name)
        if got != want:
            bad.append(f"  {name!r}: got {got}, expected {want} — {why}")

    # ⚠️ THE REGRESSION. Before the fix this returned 121356 and the nightly
    # wrote a 2026 season onto a man who last played in 1952.
    check("Hector Rodriguez", None,
          "two men share the name; the accented spelling must still count as a "
          "match, leaving the search ambiguous rather than picking the namesake")

    # The control that always behaved, and shows the fix didn't change it.
    check("Eliezer Alfonzo", None, "two exact matches — ambiguous, as before")

    # A single hit is still accepted; the fix must not make everything ambiguous.
    check("Bobby Miller", 676272, "one result, accepted directly")
    check("Hogan Harris", 663687, "one result, accepted directly")

    # No hits at all.
    check("Nobody At All", None, "empty result")
    check("", None, "empty query short-circuits")

    if bad:
        print("FAILED:")
        print("\n".join(bad))
        return 1
    print("checked 6 name searches, including the two-namesake collision")
    print("ALL PASS — an accented spelling counts as a match, so a name-twin "
          "stays ambiguous instead of resolving to the wrong man")
    return 0


if __name__ == "__main__":
    sys.exit(main())
