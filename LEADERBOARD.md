# Leaderboards: the Spinal Score

## Beat Spinal: any architecture

Spinal Score S1: Freedoom 2 MAP01-MAP04, Ultra-Violence, pistol start, real time, on-screen senses. 100% = every monster, every secret and the exit under par on every map (elite human play).

| # | Entrant | Spinal Score | Kills | Secrets | Exits | Deaths | Decision p95 | Missed tics | On intent |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Spinal (planner) | **8.3%** | 25% | 0% | 0/4 | 3/4 | 54 us | 51 | - |
| 2 | Decision model scores each move (Qwen3.5 2B) | **6.9%** | 21% | 0% | 0/4 | 0/4 | 247 ms | 17 | - |
| 3 | Starter plugin (examples/arena_agent.py) | **6.8%** | 12% | 8% | 0/4 | 4/4 | 16 us | 3 | - |
| 4 | Claude Opus 5.5 (hand-written agent) | **6.1%** | 10% | 8% | 0/4 | 4/4 | 2 ms | 11 | - |
| 5 | Fight anything in sight | **5.6%** | 17% | 0% | 0/4 | 1/4 | 9 us | 22 | - |
| 6 | Roam, no tactics | **1.1%** | 3% | 0% | 0/4 | 4/4 | - | 4 | - |

## Model league: Spinal with your model plugged in

Spinal Score S1: Freedoom 2 MAP01-MAP04, Ultra-Violence, pistol start, real time, on-screen senses. 100% = every monster, every secret and the exit under par on every map (elite human play).

| # | Entrant | Spinal Score | Kills | Secrets | Exits | Deaths | Decision p95 | Missed tics | On intent |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Claude Opus 5.5 (Spinal planner + Opus strategist) | **6.8%** | 20% | 0% | 0/4 | 2/4 | 62 us (model 7.2 s) | 49 | - |
| 2 | qwen3.5:9b (Ollama) | **5.6%** | 17% | 0% | 0/4 | 1/4 | 12 us | 9 | 100% |

## Enter

Run `relaymcp arena run --board reflex --agent plugin:my_agent.py:Agent --name "..."` (your architecture) or `--board models --agent compiled --model openai:<model>` (your model), then open a pull request with the JSON it writes under `leaderboards/`. Maintainers re-run the top entries. Agents may use only what the game hands them (the state and the screen); reading the game's files is not allowed.
