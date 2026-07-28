#!/usr/bin/env python3
"""Build golden_baseline.json from N (>=2) independent battery runs against current
main. A cell KEY whose value differs across ANY pair of runs is UNSTABLE (model
non-determinism — chiefly questions the model sometimes routes to cannot_answer,
which is never cached and so re-translates every run; and tool_name/understood_as,
which record the model's RAW pick even when the guard makes the user-facing outcome
deterministic). Unstable keys are recorded per question and EXCLUDED from the diff
gate (advisory only). More runs -> a tighter estimate of the flaky set.

Usage: python3 build_golden.py results_A.json results_B.json [results_C.json ...]
Writes golden_baseline.json (last run's values) and prints the unstable summary.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "golden_baseline.json")


def main():
    runs = [json.load(open(p)) for p in sys.argv[1:]]
    assert len(runs) >= 2, "need >=2 result files"
    ids = sorted(set().union(*[set(r) for r in runs]))
    last = runs[-1]
    questions = {}
    total_unstable = 0
    unstable_by_key = {}
    unstable_qs = []
    n_cannot_answer = 0
    for qid in ids:
        cells = [(r.get(qid) or {}).get("cell", {}) for r in runs]
        keys = sorted(set().union(*[set(c) for c in cells]))
        unstable = [k for k in keys if len({repr(c.get(k)) for c in cells}) > 1]
        rec = next(r[qid] for r in reversed(runs) if qid in r)
        goldcell = (last.get(qid) or {}).get("cell") or cells[0]
        # RULE (not observation): a question whose translation was cannot_answer in
        # ANY run is model-non-deterministic by construction — cannot_answer is never
        # cached, so it re-translates every run and can flip to a real tool at any
        # time (or vice versa). Flag ALL its cells advisory so the gate is stable
        # regardless of which flaky questions happen to wobble on a given run, not
        # just the ones observed to differ across these N baseline runs.
        cannot_answer = any(c.get("tool_name") == "cannot_answer" for c in cells)
        if cannot_answer:
            n_cannot_answer += 1
            unstable = keys
        questions[qid] = {"q": rec["q"], "bucket": rec["bucket"],
                          "cell": goldcell, "unstable": unstable,
                          "cannot_answer_class": cannot_answer}
        if unstable:
            total_unstable += len(unstable)
            unstable_qs.append((qid, rec["q"], unstable))
            for k in unstable:
                unstable_by_key[k] = unstable_by_key.get(k, 0) + 1

    golden = {"meta": {"n_runs": len(runs), "n_questions": len(ids),
                       "n_cannot_answer_class": n_cannot_answer,
                       "n_gated_questions": len(ids) - len(unstable_qs),
                       "n_unstable_cells": total_unstable,
                       "n_unstable_questions": len(unstable_qs),
                       "unstable_by_key": unstable_by_key},
              "questions": questions}
    json.dump(golden, open(OUT, "w"), indent=2, ensure_ascii=False, sort_keys=True)
    print(f"wrote {OUT}")
    print(f"questions: {len(ids)}")
    print(f"UNSTABLE questions: {len(unstable_qs)}  (cells: {total_unstable})")
    print(f"unstable by key: {unstable_by_key}")
    for qid, q, keys in unstable_qs:
        print(f"  {qid} {keys}  {q[:60]}")


if __name__ == "__main__":
    main()
