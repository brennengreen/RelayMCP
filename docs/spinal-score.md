# The Spinal Score

A benchmark for real-time game agents that is meant to stay hard. Season 1 numbers are on the
[live leaderboard](https://brennengreen.github.io/spinal/) and in [LEADERBOARD.md](../LEADERBOARD.md).

## The test

- **Levels:** Freedoom: Phase 2 MAP01-MAP04 (free Doom II levels that ship in ViZDoom's wheels), on Ultra-Violence,
  from a pistol start, one attempt per map.
- **Real time:** the game runs at 35 tics a second whatever the agent does. A slow decider misses tics; decision
  latency and missed tics are reported with every score.
- **Senses:** the agent's own state (health, ammo, position, angle, kills, secrets found), the objects on screen
  (as a perfect detector would report them: name, distance, bearing), the screen's pixels and the level's layout.
  Nothing behind a wall: agents remember what they saw. Reading the game's files is not allowed.
- **Score:** Doom's own tally for each map, the way expert players rate a run: kills % (monsters killed out of those
  placed for Ultra-Violence; lost souls don't count, as in Doom), secrets %, and the exit against par (par / time;
  0 without an exit). An attempt ends at the exit, at death or at 3 x par. The Spinal Score is the mean of the three
  parts over the four maps.

Watch any agent play it in a window, with its goals as game messages: `spinal arena run --show` (add
`--agent plugin:my_agent.py:Agent` for yours). Watched runs are not ranked: a window renders differently.

## What 100% means

Every monster, every secret and the exit under par, on every map, in real time, from what is on screen: "UV-Max
under par", the standard of elite Doom players. An expert who maxes every map at twice par scores 83%. If an agent
ever reaches 100% on levels it has never seen, it plays a fast shooter at an elite human level.

## Why it is built this way

A benchmark that today's agents already ace measures their blind spots, not the game: when current agents score
above about 80% at design time, the test was shaped around what they do well. So:

- **Spinal does not get its home turf.** Spinal was developed on ViZDoom's deathmatch arena; that arena is practice
  and is not scored. The season's levels were not used to build any agent here. Spinal's planner scores 8.3%; a
  20-line reactive agent ([examples/arena_agent.py](../examples/arena_agent.py)) scores 6.8% and finds a secret
  Spinal never has.
- **The scale is the game's, not ours.** Kills, secrets and par times come from the levels themselves (checked
  against the game data in `tests/test_arena.py`), so no number was tuned to make any entrant look good.
- **No wallhacks.** Seeing monsters through walls would make kills and survival far easier than they are for a
  person, so a score near 100% would not mean what it says.
- **Seasons get harder.** If any entry passes 80%, the next season adds levels it has never seen and takes senses
  away (the layout first, then the object detector, leaving the pixels).

## Known limits (season 1)

- The layout is given, as for a player who studied the automap; it can hint at secret areas.
- Four maps and one attempt per map: scores are noisy by a few points. Maintainers re-run the top entries. Measured
  (October 2026, three real-time runs each): Spinal's planner 8.3%, 11.0%, 17.8% (the last found a secret on MAP02);
  the planner with Claude Opus 5.5 setting its goals 6.8%, 6.4%, 10.4%; a hand-written plugin 6.1%, 7.2%, 6.2%. One
  secret can move a run by 5 points or more, so compare entrants on several runs.
- Single player against Doom's monsters, not other players. Head-to-head play is a later season.
