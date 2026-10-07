# Learning any game from practice: literature review and proposed approach

October 2026. Four parallel reviews (code-writing agents; generalist game agents and world models; learning a game's
rules by experiment; scaling and distillation), every source checked against its arXiv abstract or official page
unless marked secondary. Goal: an automated, general way for agents to learn to play games at a human level in real
time, whose gains keep compounding with more data and compute, as a path to embodied and physical AI.

## What the field has shown

| Thread | Best results | What is still missing |
|---|---|---|
| Generalist game agents | SIMA 2 (DeepMind, [2512.04797](https://arxiv.org/abs/2512.04797)) near-novice on held-out games and some self-improvement in one game; Lumine ([2511.08892](https://arxiv.org/abs/2511.08892)) real-time, hours-long missions zero-shot in same-genre games; Game-TARS ([2510.23691](https://arxiv.org/abs/2510.23691)) "close to fresh humans" but with the game paused; NitroGen ([2601.02427](https://arxiv.org/abs/2601.02427)) a fast system-1 from 40k hours of video, no planning | No verified human-level real-time play on unseen games. No agent improving from its own practice across games with a measured curve. Latency forces small models (SIMA 2 uses Flash-Lite) or paused games |
| World models | Dreamer 4 ([2509.24527](https://arxiv.org/abs/2509.24527)) learns Minecraft inside its model (diamonds in 0.7% of episodes); Genie 3 consistent for minutes; GameNGen neural Doom | Learned simulators are uninterpretable, unverified against the true rules and drift; no agent improves its own competence by stepping through them across many games |
| LLMs writing game code | Voyager skill libraries (API, not pixels); Eureka rewards; PoE-World ([2505.10819](https://arxiv.org/abs/2505.10819)) code world models that generalise to unseen Pong and Montezuma levels; Code to Control ([2609.38733](https://arxiv.org/abs/2609.38733)) code controllers competitive with deep RL; AutoHarness ([2603.03329](https://arxiv.org/abs/2603.03329)) | Nearly all use object-level state, not pixels (PoE-World needed per-game parser fixes); train and test on the same tasks; **no curve of held-out performance against practice or compute** |
| Learning rules by experiment | EMPA ([2107.12544](https://arxiv.org/abs/2107.12544)) matches human learning speed on 90 grid games, given object state and a hand-built rule language; OneLife ([2510.12088](https://arxiv.org/abs/2510.12088)); executable world models on ARC-AGI-3 ([2605.05138](https://arxiv.org/abs/2605.05138)); GPT-6 Astra 62.7% standard / 99.9% custom harness on ARC-AGI-3 ([arcprize.org/blog/astra](https://arcprize.org/blog/astra)) | All turn-based or object-level. Nobody combines experiment-driven code world models with a fast real-time controller in visually complex games. AutumnBench ([2510.19788](https://arxiv.org/abs/2510.19788)): humans beat frontier models, blamed on exploration and belief updates |
| Scaling and distillation | Power laws for in-distribution loss and return (Tuyls [2307.09423](https://arxiv.org/abs/2307.09423), Hilton [2301.13442](https://arxiv.org/abs/2301.13442), Pearce [2411.04434](https://arxiv.org/abs/2411.04434)); held-out generalisation scales with the **diversity of environments**, not samples (Procgen, AdA, Kinetix, Lin et al. [2410.18647](https://arxiv.org/abs/2410.18647), pi0.5) | Distilling a symbolic expert into a network hits the expert's ceiling (NetHack, [2305.19240](https://arxiv.org/abs/2305.19240)) unless RL or search sits on top (ExIt, AlphaZero). The loop "LLM writes programs -> data -> network -> network's failures back to the LLM" has not been studied; nor held-out scaling against generator diversity |
| Games -> physical AI | Waymo's world model is built on Genie 3; NitroGen reuses GR00T's design; Game2Policy ([2609.18650](https://arxiv.org/abs/2609.18650)) +18 points on real robots from VR game data | No direct evidence that game-playing skill transfers to robot control: the transfer so far is of recipes and world models |

## Our own evidence (spinal learn, Opus 5.5 writing the agent)

- v1 (whole rewrites, one fixed practice set): Doom practice doubled (4.6% -> 10.9%) while never-practised maps stayed
  at 3-4%: it memorised the maps. Breakout reached about 61% on unseen seeds.
- v2 (fresh games every round, failure drills, edits, paired statistical acceptance): Breakout reached **82.4% on
  unseen seeds** by round 12 (v1: 61%) and **88.0% on the held-out seeds**, then plateaued for the last 9 of its 16
  rounds. Doom stayed at **about 3%** on unseen maps for all 16 rounds while practice rose to 7.2%: candidates fix real
  problems (slime floors, unseen attackers, targeting) but the fixes do not transfer.
- Reading the tries: where perception is easy and the rules are simple (Breakout), writing the controller is enough.
  Where the agent must understand a world it only partly sees (Doom: hazards, doors, keys, who is shooting), editing a
  controller is the wrong unit of learning: it never builds a model of the world it could test or reuse.

## The gap

Every thread fails at the same junction. Systems that **understand a world** (code world models, theory-based agents)
need clean object states and act slowly. Systems that **act in real time from pixels** (VLA agents, deep RL, behaviour
cloning) don't build checkable understanding and don't improve from their own practice across games. And no one
measures what matters for the goal: how performance on **games and levels never practised** grows with practice and
compute.

## Proposed approach: the Scientist-Compiler loop

A frontier model never plays. It acts as a scientist and a compiler, turning practice into four kinds of verified code;
fast code and a small network do the playing.

1. **Perception code (pixels -> objects).** The model writes extractors and checks them by self-consistency (objects
   persist, move continuously, explain the reward and the life counter) and, during development, against the
   emulator's ground truth (ALE memory, ViZDoom state), so their accuracy is measured, not assumed.
2. **A code world model, learned by experiment.** The model states rules as small programs, each with a prediction,
   and designs experiments to test them: from saved game states it runs a probe program and compares what happens with
   what the rule predicted. Rules are kept only if they predict **held-out transitions** better than without them (as
   a product of small experts, as in PoE-World, but from our own perception and with active experiments instead of a
   demonstration). Its error on real play is measured continuously: that number *is* the sim-to-real gap.
3. **A fast planner and controller.** Plans through the code world model (model-predictive control or search) inside
   the frame budget. This is the expert that ExIt and AlphaZero show can be improved on.
4. **A neural student trained in the verified model.** Unlimited data from planning inside the code world model, plus
   RL in the real game, and DAgger with the planner as teacher. The student's real-game failures become drills; drills
   the code world model mispredicts send the scientist back to step 2. This is how to get past the distillation
   ceiling seen in NetHack: the teacher keeps improving because the simulator it plans in keeps improving.

**Across games**, perception and rule modules go into a shared library (gravity, collisions, projectiles, pickups,
health, doors). The literature says held-out generalisation scales with environment diversity, so the library is the
compounding asset: each new game should take fewer model calls, experiments and frames to learn.

**Why this is new.** Pieces exist separately (PoE-World, EMPA, WorldCoder for world models; Dreamer 4 for training
inside a model; ExIt and DAgger for distillation; Voyager for libraries). Not shown anywhere: experiment-driven code
world models **from pixels** in **real-time** games, used as a verified simulator to train a fast neural policy, with
a **cross-game library**, judged by **held-out practice curves**.

**Why it should compound rather than plateau.** It has four independent ways to grow, each with published scaling
evidence: (a) more games -> a larger library and better transfer (diversity laws); (b) more compute for search and RL
in the verified simulator (AlphaZero-style power laws); (c) more experiments -> a lower model error, measurable
directly; (d) a larger student trained on more simulated data (imitation-learning power laws). The v1 and v2 plateaus
came from having only one way to grow: editing one program.

**Why it matters for physical AI.** A robot has no save and reload, but it can do what this loop does: hypothesise a
rule, run a cheap experiment, keep a checkable world model, and practise inside it. Games make every part of that
measurable against ground truth. What should transfer is the method of discovering a world's rules quickly, not game
physics.

## Testable claims and stop rules

| # | Claim | Test | Continue if |
|---|---|---|---|
| H1 | Code world models can be built from pixels by experiment | 12 Atari games: one-step prediction error of positions and rewards on held-out transitions, against ground truth | Better than a no-change baseline by at least 50% on 8 of 12 games |
| H2 | Planning through them beats the code-policy learner | Same model-call budget, unseen seeds; human-normalised score | Wins on most games; and on Doom, the unseen-map score moves off its 2.6% floor |
| H3 | Knowledge transfers between games | Held-out games learned with the library and without it | 25% fewer model calls or frames to reach the same score |
| H4 | The student scales | Unseen-game score as simulated data doubles 3 times, and the student against its teacher | Keeps rising each doubling and ends above the teacher |
| Overall | No early plateau | Score on unseen games against total compute, log scale | The slope stays above a quarter of its early value over 3 doublings |

**Benchmark contribution.** A "held-out practice curve" protocol: real time, from the screen, games and levels never
seen in practice, score against compute, with human first-session learning curves for comparison (what humans learn
in minutes, Tsividis 2017).

## Plan

1. Build the measurement harness: 8 practice + 4 held-out Atari games, plus Doom MAP05-16; ground-truth adapters
   (ALE memory, ViZDoom state); human-normalised scoring. About 4 agent-hours.
2. H1: perception and an experiment-driven world model. About 6-8 agent-hours plus runs.
3. H2: a planner over the world model, compared with v2 at equal model calls.
4. H3: the shared library across games.
5. H4: the neural student (small CNN; local first, a GPU past about 1M frames).

Main risks: perception from pixels in 3D (Doom's sprites are provided by the game, Atari's are not); planning in time
for real-time play; and model cost (a full run is 150-250 frontier-model calls). The ARC-AGI-3 harness result (62.7%
vs 99.9% for the same model) suggests the scaffolding matters as much as the model, which is what this project builds.

## Results, first run (October 2026)

Code: `src/relaymcp/play/scientist.py` (H1, H3), `planner.py` (H2), `student.py` (H4). All four use true objects from
the console's memory (OCAtari) as perception; perception from pixels is not tested yet. Opus 5.5 throughout.
Atari episodes in H2 and H4 follow the standard protocol: 5 minutes, 25% sticky buttons, seeds never practised;
scores are % of the human reference (Mnih et al. 2015) above random play.

**H1: passed.** 8 rounds of 2 candidates (16 calls) per game, 3 experiments a round from saved moments. One-step
error on recordings from unseen seeds, as % of the nothing-moves baseline: Freeway 1%, Pong 8%, Breakout 14%, Asterix
18%, Space Invaders 23%, Kangaroo 32%, Seaquest 36%, Skiing 37%, Ms. Pac-Man 38%, Frostbite 39%, Boxing 46%,
BankHeist 58%. **11 of 12 under 50%** (gate: 8), and below the keep-velocity baseline on **12 of 12**. The models hold
up when fed their own predictions: after 8 steps open-loop, Freeway is at 1% and Pong at 24% of the baseline's error.

**H2: failed as designed.** At equal calls (12 per game), the frontier model writing the policy directly is strong:
Breakout 1204%, Asterix 219%, Boxing 101%, Freeway 96%, Seaquest 73%, Space Invaders 57%, Pong 50%, Ms. Pac-Man 32%
of human. Writing only an objective for a planner over the learned world model beats it on 2 of 8 games (Asterix 449%,
Freeway 108%); mean 89% against 229%. A hybrid, a policy that may call the world model, beats it on 3 of 8 (Asterix
1355%, Pong 72%, Freeway 108%). Reading the tries: objectives are a clumsy way to state a strategy (the model ends up
writing a ball-intercept predictor inside value()), and where true objects are given and the rules are simple, a
written policy is already superhuman, so lookahead adds little. The test that matters is still Doom.

**H3: failed.** A 37,000-character library distilled from the 8 practice games' world models, offered to the 4
held-out games: BankHeist reached its from-scratch final error in 5 rounds instead of 8, Frostbite started faster but
ended level, Kangaroo got worse (71% against 38% of the baseline: the library anchored it), Skiing slightly worse. One
run per arm. The library needs retrieval of the relevant parts, not all of it.

**H4: partly passed.** A 2-layer numpy MLP over encoded objects, trained by DAgger on the Breakout policy (teacher
1204%): 2.5k labels -6%, 5k -6%, 10k 4%, 20k 14%, 40k 67%, 80k 164%, **160k 461%**, 320k 366% of human (4 seeds,
high variance), at about 25 microseconds a decision. It rises across 4 consecutive doublings, but stays well below its
teacher. Training in the learned world model instead of the real game reached 28% at 40k (real: 67%).

**Reading.** Learning a game's rules by experiment works across many games, and a fast student keeps improving as
data doubles. Planning through the rules only beats a written policy where the strategy is hard to write directly;
Doom, with partial observation, is the next test. Next: perception from pixels (Opus writes it, checked against the
console's memory), a Doom version of H1, the student on more games with more seeds, and a retrieved library.
