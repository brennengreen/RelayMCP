# Leaderboards: the Spinal Score

## Beat Spinal: any architecture

Spinal Score S1: Freedoom 2 MAP01-MAP04, Ultra-Violence, pistol start, real time, on-screen senses. 100% = every monster, every secret and the exit under par on every map (elite human play).

| # | Entrant | Spinal Score | Kills | Secrets | Exits | Deaths | Decision p95 | Missed tics | On intent |
|---|---|---|---|---|---|---|---|---|---|

## Model league: Spinal with your model plugged in

Spinal Score S1: Freedoom 2 MAP01-MAP04, Ultra-Violence, pistol start, real time, on-screen senses. 100% = every monster, every secret and the exit under par on every map (elite human play).

| # | Entrant | Spinal Score | Kills | Secrets | Exits | Deaths | Decision p95 | Missed tics | On intent |
|---|---|---|---|---|---|---|---|---|---|

## Enter

Run `relaymcp arena run --board reflex --agent plugin:my_agent.py:Agent --name "..."` (your architecture) or `--board models --agent compiled --model openai:<model>` (your model), then open a pull request with the JSON it writes under `leaderboards/`. Maintainers re-run the top entries. Agents may use only what the game hands them (the state and the screen); reading the game's files is not allowed.
