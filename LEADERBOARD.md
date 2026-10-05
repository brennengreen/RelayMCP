# Leaderboards: the Spinal Score

## Beat Spinal: any architecture

Spinal Score S1: Freedoom 2 MAP01-MAP04, Ultra-Violence, pistol start, real time, on-screen senses. 100% = every monster, every secret and the exit under par on every map (elite human play).

| # | Entrant | Spinal Score | Kills | Secrets | Exits | Deaths | Decision p95 | Missed tics | On intent |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Spinal (planner) | **6.8%** | 20% | 0% | 0/4 | 4/4 | 168 us | 170 | - |
| 2 | Starter plugin (examples/arena_agent.py) | **5.9%** | 9% | 8% | 0/4 | 4/4 | 23 us | 4 | - |
| 3 | Claude Opus 5.5 (hand-written agent) | **3.8%** | 11% | 0% | 0/4 | 3/4 | 2 ms | 9 | - |
| 4 | Fight anything in sight | **2.8%** | 8% | 0% | 0/4 | 3/4 | 12 us | 5 | - |
| 5 | Decision model scores each move (Qwen3.5 2B) | **2.2%** | 7% | 0% | 0/4 | 3/4 | 911 ms | 973 | - |
| 6 | Roam, no tactics | **1.1%** | 3% | 0% | 0/4 | 4/4 | - | 4 | - |

## Model league: Spinal with your model plugged in

Spinal Score S1: Freedoom 2 MAP01-MAP04, Ultra-Violence, pistol start, real time, on-screen senses. 100% = every monster, every secret and the exit under par on every map (elite human play).

| # | Entrant | Spinal Score | Kills | Secrets | Exits | Deaths | Decision p95 | Missed tics | On intent |
|---|---|---|---|---|---|---|---|---|---|
| 1 | qwen3.5:9b (Ollama) | **3.6%** | 11% | 0% | 0/4 | 2/4 | 41 us | 534 | 100% |

## Enter

Run `relaymcp arena run --board reflex --agent plugin:my_agent.py:Agent --name "..."` (your architecture) or `--board models --agent compiled --model openai:<model>` (your model), then open a pull request with the JSON it writes under `leaderboards/`. Maintainers re-run the top entries. Agents may use only what the game hands them (the state and the screen); reading the game's files is not allowed.
