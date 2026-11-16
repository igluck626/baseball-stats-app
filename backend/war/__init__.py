"""Our own WAR (wins above replacement), version 1.

The calculation reads only our own data: the season counting stats in our tables, inputs derived from Retrosheet
play-by-play (historical seasons, built on a machine that has the Retrosheet DuckDB) and from balldontlie (current
seasons), and the frozen settings in ``params/war_params_v1.json``. It never reads the stored Baseball-Reference WAR
columns or anything derived from them; ``backend/tests/test_war_v1_package.py`` checks that no file here names them.

Modules
  compute.py      the calculation: inputs → per player-season WAR pieces (pure; writes nothing)
  parks.py        batting / pitching park factors from our game results
  fld_totals.py   fielding from season totals, for seasons with no play-by-play
  common.py       team-code normalising and batted-ball helpers shared by the builders
  dbconn.py       a read-only database connection for local runs
  builders/       scripts that produce the input files compute.py reads (see README.md)
"""
