# Fast decisions and vision

Real-time play has three speeds. The handheld's behaviors react in 1-35 ms and the planning model takes 11-16 s a
step. Between them is tactics: choosing which skill runs next when something changes, a few to a hundred times a
second, without waiting for the planner. `src/relaymcp/host/decide.py` is that layer: `decide(question, state)`
picks one answer from a fixed list, with the same call for every backend.

## What works (measured on an M4 Mac, 2026-10-03)

**Compile the intent, don't consult a model per decision.** A capable model turns a plain-English intent ("retreat
from a creeper within 6 blocks; otherwise fight anything within 5; ...") into a small Python policy once
(`compile_policy`). The policy is checked before use: no imports or private names, only safe builtins, and it must
answer every sample state with one of the answers. It then decides in microseconds, exactly:

| Approach (160 balanced states, 4 answers) | Agreement with the intent | Per decision |
|---|---|---|
| Qwen3 0.6B, one forward pass over the answer letters (prompt cached) | 25% (chance) | 45-90 ms |
| LFM2.5 1.2B, the same | 25-28% | 82-117 ms |
| Qwen3.5 2B, the same, state given as nearest distances | 60% | ~245 ms |
| ... with 32 worked examples cached in the prompt | no better | slower |
| Intent compiled to a policy by qwen3.5:9b (local, Ollama) | **100% of 5,000 states, two different intents** | **2-3 us** |

Running a model as a decision function (one pass, no text generated, the fixed prompt cached) is what makes decision
APIs fast, and it is not a new model. But a model small enough to answer in milliseconds can't apply a multi-rule
intent in one pass, and a big enough one costs hundreds of milliseconds, like the hosted APIs (~150 ms). A compiled
policy keeps the model's understanding of the intent and runs at the speed of code. Re-tasking compiles again
(45-55 s with the local 9B model on a busy Mac; seconds with a hosted model) while the old policy keeps playing.

**Vision: embed each frame once, answer bounded questions with tiny heads.** On 24 hand-labeled frames from the
handheld (gameplay, pause menu, death screen, other menus):

| Approach | Right | Per frame |
|---|---|---|
| SmolVLM 256M, one pass over the answer letters | 14/24 (always "gameplay") | 1.6 s |
| LFM2.5-VL 1.6B, the same | 9/24 | 0.72 s |
| 32x18 pixel thumbnails, nearest neighbour | 19/24 | microseconds |
| CLIP ViT-B/32 embedding + text prompts (zero-shot) | 21/24 | 19 ms |
| CLIP ViT-B/32 embedding + nearest labeled class (few-shot) | **23/24** | **19 ms + 7 us per question** |

Small vision-language models are too slow and too unsure for frames. One image embedding per frame (19 ms with
preprocessing, on the Mac's GPU) answers any number of bounded questions for microseconds each, from text prompts
alone or a handful of labeled frames.

## Next

- A tactics loop on the Mac: telemetry (or frame embeddings) in, `decide` with a compiled policy, start or replace
  the handheld's behavior program; the planner and voice change the intent.
- Frames streamed from the handheld to the Mac (Phase 2): embeddings for state and bounded questions at 20-50 fps;
  where things are (mob bearings) needs a detector, trained on frames labeled from game telemetry.

## Reproducing

```sh
python scripts/decide/tactics.py --models mlx-community/Qwen3-0.6B-4bit --formats json,facts,features   # needs mlx-lm
python scripts/decide/compile_policies.py qwen3.5:9b                                                      # needs Ollama
```
