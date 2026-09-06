"""Where a substitute batted — the one rule, kept in one place.

A box score sets a pinch hitter in UNDER the man he replaced rather than
listing him as a tenth batter, and that needs his batting slot. Neither
`/lineups` (the starting nine only) nor `/stats` (no order at all) carries
it, so it is derived from the order in which men came to the plate.

⚠️ THIS RULE EXISTS TWICE. The Swift copy is `substituteBattingOrders` in
BaseballStats/Models/Scores.swift, which serves a finished game the app
fetches itself; this copy serves a game in progress, whose box score is
assembled here and shipped ready-made. They fill ONE field that ONE view
reads (`stats_battingOrder`, read by `BoxScoreView.substitutionDepth`), so
they must produce identical codes. Change one, change the other.

What keeps them honest is `testdata/box-score-order.json`, which holds real
captured payloads AND the codes both are expected to produce. Neither suite
owns those expectations and neither imports the other; either one drifting
fails against the same file.

This module is deliberately PURE — standard library only, no config, no
network, no database. That is what lets a test import it directly instead of
dragging in the service that uses it.
"""

from typing import Optional


def _rotation_walk_slots(seq: list[int], declared: dict[int, int]) -> Optional[dict[int, int]]:
    """Slot for every substitute, proven by the whole side at once: walk the
    sequence against a 1..9 rotation and require every already-known batter to
    land where it says. None the moment one doesn't — past that point every
    slot would be silently wrong."""
    out: dict[int, int] = {}
    expected = 1
    for pid in seq:
        d = declared.get(pid)
        if d is not None:
            if d != expected:
                return None
        elif pid in out:
            if out[pid] != expected:
                return None
        else:
            out[pid] = expected
        expected = expected % 9 + 1
    return out

def _bracketed_slots(seq: list[int], declared: dict[int, int]) -> dict[int, int]:
    """Slot for each substitute the side can prove INDIVIDUALLY, for when the
    sequence has a fault somewhere and the walk above has refused it.

    Take the nearest batter with a known slot on each side, rotate each by its
    distance, and accept only when the two independently agree. A fault between
    the man and either neighbour makes them disagree, which is what makes the
    agreement a proof. A man with a neighbour on only one side is declined —
    one anchor cannot be contradicted, and mid-game that is exactly the batter
    at the end of the sequence.

    Mirrors `bracketedSlots` in Scores.swift; the same unbounded hole applies
    (two faults of opposite sign bracketing one man would cancel and agree)."""
    def rotate(slot: int, offset: int) -> int:
        return ((slot - 1 + offset) % 9 + 9) % 9 + 1

    known = [declared.get(pid) for pid in seq]
    votes: dict[int, set] = {}
    vetoed: set = set()
    for i, pid in enumerate(seq):
        if declared.get(pid) is not None:
            continue
        back = forward = None
        for d in range(1, 10):
            if back is None and i - d >= 0 and known[i - d] is not None:
                back = rotate(known[i - d], d)
            if forward is None and i + d < len(known) and known[i + d] is not None:
                forward = rotate(known[i + d], -d)
            if back is not None and forward is not None:
                break
        if back is None or forward is None:
            continue
        if back == forward:
            votes.setdefault(pid, set()).add(back)
        else:
            vetoed.add(pid)
    return {pid: next(iter(v)) for pid, v in votes.items()
            if pid not in vetoed and len(v) == 1}

def _slot_codes(pas: list[dict], lineup: Optional[list[dict]], team_id,
                half: str, stats_pa: int, is_final: bool) -> dict[int, int]:
    """MLB-style `slot * 100 + depth` for one side — "600" the number-six
    starter, "601" the first man to bat in his slot — so a substitute renders
    indented under the man he came in for. Same encoding the historical box
    score ships and the same the finals path derives in Scores.swift; the two
    must agree, because the client reads them through one field.

    A man with no code is appended, which is what the live box score did
    before this existed. That is the safe outcome and this reaches for it
    whenever the alternative would be a guess: a wrong slot reads as fact,
    an append visibly doesn't."""
    declared: dict[int, int] = {}
    for row in (lineup or []):
        pid = (row.get("player") or {}).get("id")
        slot = row.get("batting_order")
        tid = (row.get("team") or {}).get("id")
        if pid is not None and slot is not None and tid == team_id:
            declared[pid] = slot
    if set(declared.values()) != set(range(1, 10)):
        return {}

    rows = [p for p in pas if (p.get("half_inning") or "").lower() == half]
    rows.sort(key=lambda p: (p.get("inning") or 0, p.get("pa_number") or 0))
    seq = [p.get("batter_id") for p in rows if p.get("batter_id") is not None]
    if len(seq) != len(rows) or not seq:
        return {}

    # The count check, in its live-aware form.
    #
    # On a finished game the PA rows must reconcile EXACTLY with the summed
    # plate appearances on this side's stat lines — measured over 116 sides,
    # they disagree on about a fifth, in both directions.
    #
    # Mid-game one side always runs a row ahead, because BDL opens the PA row
    # for the man at the plate before his stat line counts him. Measured on a
    # game in progress: the batting side +1, the fielding side 0. A strict
    # equality check would therefore refuse every live game outright. The
    # surplus row is ALWAYS the last one, so dropping it costs nothing before
    # it — the man at the plate simply waits for his plate appearance to
    # finish before he can be placed, which is the accepted behaviour anyway.
    surplus = len(seq) - stats_pa
    if surplus == 0:
        trusted = seq
    elif surplus == 1 and not is_final:
        trusted = seq[:-1]
    else:
        trusted = None

    walk = _rotation_walk_slots(trusted, declared) if trusted is not None else None
    basis = trusted if trusted is not None else seq
    derived = walk if walk is not None else _bracketed_slots(basis, declared)

    codes = {pid: slot * 100 for pid, slot in declared.items()}
    depth: dict[int, int] = {}
    for pid in basis:
        if pid in codes or pid not in derived:
            continue
        slot = derived[pid]
        d = depth.get(slot, 0) + 1
        if d >= 100:            # would collide with the next slot's code
            continue
        depth[slot] = d
        codes[pid] = slot * 100 + d
    return codes
