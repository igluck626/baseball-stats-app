#!/usr/bin/env python3
"""How the server runs the nightly provisional WAR (backend/api/main.py → war/provisional.py in a subprocess).

⚠️ THE CHECKS THAT MATTER:
  • WAR_NIGHTLY unset (or anything but dry_run/write) means OFF: nothing runs after the nightly or the catch-up
  • the admin trigger writes only when asked to ("mode": "write") AND WAR_NIGHTLY is 'write', and only for the current
    season; anything else is a dry run; a bad season is a 400
  • a failed nightly or catch-up doesn't start the WAR step
  • a report that can't be read, a crash or a timeout is reported as an error, never as success
  • a written night clears the payload cache; a second run while one is going is turned away
  • /war/nightly-status reads the saved report (it survives a restart), with inputs_through as the freshness signal

No database, no network: the subprocess is replaced. Standalone, no pytest.
Run: <backend python> backend/tests/test_war_nightly_wiring.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "api"), os.path.join(HERE, ".."), os.path.join(HERE, "..", "scripts")]
os.environ["DATABASE_URL"] = "postgresql://x@localhost/x"           # unreachable on purpose
STORE = tempfile.mkdtemp(prefix="war_store_")
os.environ["WAR_STORE"] = STORE
os.environ.pop("WAR_NIGHTLY", None)
import main  # noqa: E402
from war import provisional  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  ({detail})"))


class Fake:
    def __init__(self, out='', err='', code=0, exc=None):
        self.out, self.err, self.code, self.exc, self.calls = out, err, code, exc, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.code, self.out, self.err)


print('the switch')
check('unset means off', main._war_nightly_mode() == 'off')
for v, want in (('dry_run', 'dry_run'), (' WRITE ', 'write'), ('yes', 'off'), ('', 'off')):
    os.environ['WAR_NIGHTLY'] = v
    check(f'WAR_NIGHTLY={v!r} → {want}', main._war_nightly_mode() == want, main._war_nightly_mode())
os.environ.pop('WAR_NIGHTLY')
f = Fake(out=json.dumps({'status': 'written'}))
main.subprocess.run = f
check('off: a run does nothing', main._run_war_nightly('nightly') == {'status': 'off'} and not f.calls)
n = len(main.threading.enumerate())
main._start_war_nightly('nightly')
check('off: no thread is started after the nightly', len(main.threading.enumerate()) == n)

print('a run')
os.environ['WAR_NIGHTLY'] = 'dry_run'
f = Fake(out='some log line\n' + json.dumps({'status': 'dry_run', 'season': 2027, 'measures': {'inputs_through': '2027-05-01'}}))
main.subprocess.run = f
rep = main._run_war_nightly('nightly')
cmd, kw = f.calls[0]
check('runs `python -m war.provisional --mode dry_run --root <WAR_STORE>` from backend/',
      cmd[1:] == ['-m', 'war.provisional', '--mode', 'dry_run', '--root', STORE]
      and kw['cwd'] == os.path.dirname(os.path.dirname(os.path.abspath(main.__file__))), (cmd, kw.get('cwd')))
check('in its own process with a timeout', kw.get('timeout') == main._WAR_NIGHTLY_TIMEOUT_S)
check('the report is the last line of its output', rep['status'] == 'dry_run' and rep['season'] == 2027, rep)
main.subprocess.run = Fake(out='Traceback ...', err='boom', code=1)
check('no readable report → error, with the stderr tail', main._run_war_nightly('nightly')['status'] == 'error')
main.subprocess.run = Fake(exc=subprocess.TimeoutExpired('x', 1))
check('a timeout → error', 'timed out' in main._run_war_nightly('nightly')['error'])
saved = json.load(open(os.path.join(STORE, 'reports', 'latest.json')))
check('...and the error is saved as the latest report (monitoring sees it, not last night\'s result)',
      saved['status'] == 'error' and 'timed out' in saved['error'] and saved.get('finished'), saved)
main._cache.set('k', 'v', 600)
main.subprocess.run = Fake(out=json.dumps({'status': 'unchanged'}))
main._run_war_nightly('catchup')
check('an unchanged night leaves the payload cache', main._cache.get('k') == 'v')
main.subprocess.run = Fake(out=json.dumps({'status': 'written'}))
main._run_war_nightly('catchup')
check('a written night clears it', main._cache.get('k') is None)
main._war_nightly_state['running'] = True
check('a second run while one is going is turned away', main._run_war_nightly('catchup')['status'] == 'already_running')
main._war_nightly_state['running'] = False
check('the flag is released after each run', main._war_nightly_state['running'] is False)

print('after a failed nightly')
os.environ['WAR_NIGHTLY'] = 'write'
n = len(main.threading.enumerate())
main._start_war_nightly('nightly', 'boom: the batters phase failed')
check('a failed nightly doesn\'t start the WAR step', len(main.threading.enumerate()) == n)

print('the admin trigger')
from fastapi import HTTPException  # noqa: E402
f = Fake(out=json.dumps({'status': 'dry_run'}))
main.subprocess.run = f
os.environ['WAR_NIGHTLY'] = 'off'
r = main.start_war_nightly({'season': 2026, 'force': True, 'no_download': True})
time.sleep(0.3)
check('with WAR_NIGHTLY off it runs a dry run', r['mode'] == 'dry_run' and f.calls and f.calls[0][0][4] == 'dry_run', (r, f.calls))
check('rehearsal options are passed through', r['args'] == ['--season', '2026', '--force', '--no-download'], r)
os.environ['WAR_NIGHTLY'] = 'write'
r = main.start_war_nightly({})
time.sleep(0.3)
check('with WAR_NIGHTLY write, a request without "mode" is a dry run', r['mode'] == 'dry_run', r)
r = main.start_war_nightly({'mode': 'write', 'accept_change': True})
time.sleep(0.3)
check('"mode": "write" writes, accept_change passed through', r['mode'] == 'write' and r['args'] == ['--accept-change'], r)
os.environ['WAR_NIGHTLY'] = 'dry_run'
r = main.start_war_nightly({'mode': 'write', 'accept_change': True})
time.sleep(0.3)
check('a requested write is a dry run unless WAR_NIGHTLY is write (and accept_change is dropped)',
      r['mode'] == 'dry_run' and r['args'] == [], r)
os.environ['WAR_NIGHTLY'] = 'write'
for body, why in (({'mode': 'write', 'season': 2025}, 'a past season'), ({'season': 'x'}, 'a non-year'), ({'season': 99999}, 'out of range')):
    try:
        main.start_war_nightly(body)
        check(f'400 for {why}', False, 'accepted')
    except HTTPException as e:
        check(f'400 for {why}', e.status_code == 400, e.status_code)
time.sleep(0.3)

print('the status endpoint')
os.environ['WAR_NIGHTLY'] = 'dry_run'
os.remove(os.path.join(STORE, 'reports', 'latest.json'))          # the timeout check above saved one
check('no report yet → last is null', main.war_nightly_status()['last'] is None)
provisional.save_report(STORE, {'status': 'refused', 'season': 2027, 'refusals': ['x'], 'version': 'v2',
                                'measures': {'inputs_through': '2027-05-01', 'rows': 1500}, 'warnings': ['a', 'b']})
st = main.war_nightly_status()
check('reads the saved report: status, inputs_through, refusals, warning count',
      st['last']['status'] == 'refused' and st['last']['inputs_through'] == '2027-05-01'
      and st['last']['refusals'] == ['x'] and st['last']['warnings'] == 2 and st['last']['finished'], st)
check('and says which mode the server is in', st['mode'] == 'dry_run')
check('and this server\'s own last run', set(st['server_last']) == {'status', 'finished', 'reason'}, st)
provisional.save_report(STORE, {'status': 'error', 'season': 2027, 'error': 'OperationalError: connection to server at "db.internal" (10.0.0.1), user "postgres" failed'})
check('a database error shows its kind only (no host or user)', main.war_nightly_status()['last']['error'] == 'OperationalError',
      main.war_nightly_status()['last']['error'])
for i in range(65):
    provisional.save_report(STORE, {'status': 'unchanged', 'season': 2027}, keep=60)
kept = [n for n in os.listdir(os.path.join(STORE, 'reports')) if n[:1].isdigit()]
check('reports are kept to the last 60', len(kept) <= 60, len(kept))

os.environ.pop('WAR_NIGHTLY', None)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
