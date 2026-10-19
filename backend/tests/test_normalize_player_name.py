#!/usr/bin/env python3
"""`_normalize_player_name`, the balldontlie-matching rubric's name folder.

⚠️ THE CHECK THAT MATTERS: its suffixes are whole words after a space
(" jr", " ii", ...). A later module-level `_NAME_SUFFIXES` set (bare "ii",
"jr") once replaced the rubric's tuple, so "Ishii" read "ish", "J.R." read ""
and "Duncan III" depended on the set's iteration order. Real names from our
players and pitchers tables.

Standalone, no pytest. Run: <backend python> backend/tests/test_normalize_player_name.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, "..")]
os.environ.pop("DATABASE_URL", None)
import data_service as ds                                           # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


for name, want in (("Kazuhisa Ishii", "kazuhisa ishii"), ("Masato Yoshii", "masato yoshii"),
                   ("Torii", "torii"), ("J.R.", "jr"), ("Frank Duncan III", "frank duncan"),
                   ("Michael Harris II", "michael harris"), ("Fernando Tatis Jr.", "fernando tatis"),
                   ("Daniel Lynch IV", "daniel lynch"), ("Ronald Acuña Jr.", "ronald acuna")):
    got = ds._normalize_player_name(name)
    check(f"{name!r} -> {want!r}", got == want, got)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
