# 4DSynth-Nav Benchmark — Hands-off

VLM indoor-navigation benchmark on physically-accurate 4D scenes. This file is
the session-handoff: read it to pick up where the last session left off.

Code lives in `benchmark_zehao/`. Git branch: **`benchmark-multiaction`**
(commits `2ac89be`, `9b417e4`). The repo is otherwise on a detached HEAD —
stay on this branch.

---

## Environment

- **Always run inside the `vlm-jupyter` Docker container on GPU-843** — never
  run `pxr`/`omni`/`isaacsim` code on the host.
  - `ssh GPU-843` then `docker exec vlm-jupyter /isaac-sim/python.sh <script>`
  - Container is bound to physical **GPU 4** (isaac-sim 4.5.0).
- vLLM server (Qwen3-VL-30B) serves the VLM on `localhost:8300`.
- If Isaac Sim segfaults on startup (happens after the container has been up a
  long time / many runs): `ssh GPU-843 'docker restart vlm-jupyter'`, wait ~10s.
- **OptiX denoiser** — PathTracing's denoiser needs `/usr/share/nvidia/nvoptix.bin`,
  which the container image is missing (NVIDIA runtime does not auto-mount this
  96 MB data file even with `NVIDIA_DRIVER_CAPABILITIES=all`). Without it,
  renders are noisy (~17× more high-freq noise). It was `docker cp`'d in, but
  **a container restart loses it** — after any `docker restart vlm-jupyter`,
  re-run:
  ```
  ssh GPU-843 'docker cp /usr/share/nvidia/nvoptix.bin vlm-jupyter:/usr/share/nvidia/nvoptix.bin'
  ```
  (The DLSS-RR "not supported on H100" warning is a separate, unfixable Isaac
  bug affecting RayTracedLighting mode only — irrelevant to PathTracing.)
- Python syntax check: `PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m py_compile <f>`
  (the repo `__pycache__` is root-owned; the prefix dodges it).

---

## Run a single task

```
ssh GPU-843 'docker exec -e TASK_ID=case01-L2 \
  -e VLLM_URL=http://localhost:8300/v1/chat/completions \
  -e BATCH_NAME=myrun \
  vlm-jupyter /isaac-sim/python.sh \
  /home/qi/hc/Puppeteer/zehao_task/benchmark_zehao/bench_runner.py'
```

Output: `benchmark_zehao/results/<BATCH_NAME>/<LEVEL>/<TASK>_<ts>/` —
`run.log`, `results.json`, `vlm_nav_frames_fpv/`, `vlm_nav_frames_bird/`,
`vlm_nav_frames_fpv_smooth/`, plus MP4/GIF from `gen_media.sh`.

Task file: `benchmark_zehao/benchmark_tasks.json` (40 tasks, `caseNN-LN`).
This is the original, verified task set — its `agent_start` positions place
the agent in sensible, on-floor, lit room locations. **Use this file.**

### Env-var knobs (bench_runner.py)
- `MAX_VLM_CALLS` (default 50) — episode ends at min(150 steps, this).
- `RENDER_W` / `RENDER_H` (default 960×540) — render resolution.
- `ENABLE_BIRD_SMOOTH` (default 0) — bird filler frames + bird `_smooth` folder.
- `TASKS_JSON` — override the task file path.

---

## What this session built

All on branch `benchmark-multiaction`:

1. **Obstacle-runner leap fix.** The runner mesh has a baked timeline
   animation; a playing timeline free-ran during renders, teleporting the
   runner. Fix: stop the timeline + one-time render-pipeline priming.
2. **Multi-action planning.** The VLM returns a queue of up to 5 actions
   (`PLAN: MOVE_FORWARD, ...`), executed one per step; the queue aborts on
   collision / phase change / failure. ~1.8 actions/call observed.
3. **Plan-history feedback** — recent plans + outcomes fed back into the prompt.
4. **Semantic-class target dedup** (`semantic_classes.py`) — same-semantic-class
   non-target props are deactivated so "go to the bookshelf" is unambiguous.
5. **Render performance** — 960×540 (was 1920×1080, ~4.6× faster), cheap
   filler subframes, timing probes. An episode is now ~3-4 min per 15 VLM
   calls (was unrunnable / >100 min).
6. **Visibility-gated filler frames** — filler frames only render when the
   obstacle runner is in the FPV FOV cone (pure-geometry check, ~2ms/episode).
   Off-screen: filler skipped, `sim_t` still advances (no leap on reappear).
7. **Filler-frame timing model** — runner advances smoothly during VLM
   thinking; `gen_media.sh` builds video from `*_smooth` (fpv) / per-step
   (bird) frames at `SMOOTH_FPS=3` (matches `FILLER_FPS`).

---

## Batch run + aggregate metrics

`bench_batch.py` runs many tasks and aggregates a summary report.

```
# from inside GPU-843 (it docker-exec's the runner; does NOT ssh itself)
python bench_batch.py --level L2 --batch-name batch_L2
python bench_batch.py --all --batch-name full_run
python bench_batch.py --report-only --batch-name batch_L2   # re-aggregate
```

Metrics per task: SR (success), SP (subtask progress), GD (goal distance),
steps, `vlm_calls`, `actions_per_call`, timing breakdown. Aggregated into
`results/<batch>/benchmark_report.json`.

> NOTE: `bench_batch.py` has a `--ssh` arg that is currently a no-op (it
> docker-exec's directly, assuming it already runs on GPU-843). Per-task
> `timeout` is 1800s. It does not pass `MAX_VLM_CALLS` (uses default 50).
> Minor — fix if batch runs need it.

---

## Open TODOs

- **Render noise — RESOLVED.** The OptiX denoiser was silently failing because
  the container lacked `/usr/share/nvidia/nvoptix.bin`. Copying it in (see
  Environment) restored the denoiser: measured high-freq noise dropped 2.09 →
  0.12 (~17×) at the same 960×540 / `rt_subframes`, for free (no slowdown).
  Only caveat: a container restart loses the file — re-copy it (Environment).
- **Agent gets stuck (oscillation).** On some tasks the VLM repeatedly turns
  ±15° and collides without escaping — a VLM-capability issue. Decision was
  **not to intervene** in agent behaviour.
- **`full_task_gen.py`** — a geometric task generator (FOV-frustum visibility,
  on-floor start validation). Its start-generation was unreliable and is
  **shelved**; the original `benchmark_tasks.json` starts are used instead.
  The FOV/raycast visibility check could be repurposed as an informational
  audit of existing tasks.
- **L3/L4** — multi-subtask interaction tasks (PICK_UP/PUT_DOWN/TURN_ON).
  Per-design, the second axis is **subtask count** (1 vs 2-3), not instruction
  length; interaction is just a subtask carrier, not a separate axis.

---

## Key files

| File | Role |
|---|---|
| `bench_runner.py` | runs ONE task in Isaac Sim |
| `bench_batch.py` | batch orchestrator + metric aggregation |
| `bench_helpers.py` | motion sampling, metrics, prompts |
| `semantic_classes.py` | factory → semantic-class map (target dedup) |
| `full_task_gen.py` | geometric task generator (shelved) |
| `gen_media.sh` | builds MP4/GIF from rendered frames |
| `benchmark_tasks.json` | the 40-task benchmark (use this) |
