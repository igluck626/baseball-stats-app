# /ask golden battery — the diff gate for the constraint-merge

Phase 0 of merging the two constraint systems (`_detect_constraints` + the
`_ANSWERABLE` inject block, and `_guard_scoped_tool` + `_TOOL_SCOPE_CAPS`) into one
`_guard_tool`. This battery is the regression gate every later phase must pass:
each phase may change ONLY the cells on its declared changed-behavior list.

## Files

| file | role |
|---|---|
| `gen_battery.py` | regenerates `ask_battery.jsonl` (the audit's coverage + this session's cases + known bugs). Deterministic order. |
| `ask_battery.jsonl` | 179 questions: `{id, q, bucket, note}`. |
| `run_battery.py` | POSTs each question to a live `/ask`, captures the cells that matter, joins `tool_name` from `ask_log`. |
| `build_golden.py` | takes N run outputs, marks any cell that varies across runs UNSTABLE (advisory), writes `golden_baseline.json`. |
| `golden_baseline.json` | the frozen baseline (current `main`) + per-question unstable-key lists. |
| `diff_battery.py` | diffs a fresh run against golden, prints only non-advisory changes, exit 1 on any. |
| `test_guard_tool.py` | unit-tests `_guard_tool` in isolation (AST-extracts the constraint subsystem from `main.py`; no app import). |

## Run it

```bash
# tool_name comes from ask_log; export the PUBLIC db url (Postgres service DATABASE_PUBLIC_URL)
export ASK_DB_URL="postgres://...proxy.rlwy.net:.../railway"

python3 run_battery.py --out /tmp/run.json      # ~179 /ask calls, one rate-limit bucket
python3 diff_battery.py /tmp/run.json            # exit 0 = gate green

# rebuild the baseline (>=2 runs; more runs = tighter flaky-set estimate):
python3 run_battery.py --out /tmp/A.json && python3 run_battery.py --out /tmp/B.json
python3 build_golden.py /tmp/A.json /tmp/B.json

python3 test_guard_tool.py                       # unit tests, no network/db
```

## Notes

- **Rate limit.** Every call (cache hit included) counts once against the per-identity
  daily cap. All calls send a fixed `x-device-id` so they share ONE bucket. 179/run is
  fine at `ASK_DAILY_LIMIT=1000`.
- **Why cells go unstable.** Successful translations are cached (no TTL) and frozen, so
  they're stable run-to-run. A `cannot_answer` is never cached, so it re-translates every
  run — the prime source of flakiness. `tool_name`/`understood_as` record the model's RAW
  pick and can wobble even when the guard makes the user-facing outcome deterministic
  (e.g. an ambiguous-city question the model routes three ways but the guard always
  declines). Those are marked advisory; the user-facing fields (count, out_of_scope,
  declined, …) stay in the gate.
- **Volatile fields are not captured** (`cached`, timings, `query_ms`) so they can't cause
  false diffs.
- The merge does NOT change `prompt_version` (the guard is code, not schema), so post-merge
  runs reuse the same frozen translations and any diff is guard-attributable.
