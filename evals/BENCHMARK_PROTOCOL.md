# Benchmark protocol

Public benchmark results are only comparable when the Agent revision, dataset,
model, budgets, task selection and evaluator are frozen before generation. This
file defines the protocol; it does not contain unrun or estimated scores.

The published 2026-08-31 run is recorded in
[`BENCHMARK_RESULTS.md`](BENCHMARK_RESULTS.md).

## Common run manifest

Every run must record:

- Agent Git commit and whether the working tree was clean;
- model name, API base provider label and decoding parameters;
- `AGENT_MAX_STEPS`, `AGENT_MAX_TIME`, `AGENT_MAX_TASK_TOKENS` and permission mode;
- benchmark package version and exact source commit;
- ordered instance IDs and deterministic selection rule;
- start/end time, machine/OS and evaluator version;
- per-instance outcome, tokens, model calls, tool calls, checks, duration,
  termination reason and final patch/hash;
- evaluator result, false completion and any protected-file violation.

Secrets must never be written to the manifest. Failed, interrupted and
NO_PROGRESS runs stay in the denominator.

## BigCodeBench 30-case pilot

Upstream: <https://github.com/bigcode-project/bigcodebench>

BigCodeBench is primarily a function-level code-generation benchmark with
`complete` and `instruct` splits. A 30-case Agent adaptation is therefore
reported as **BigCodeBench Instruct 30-case pilot**, never as an official full
BigCodeBench leaderboard score.

Before running:

1. Pin the upstream package/source commit and dataset release.
2. Freeze the `instruct` split and the exact eligible standard-library-only
   instance IDs after inspecting the pinned dataset metadata.
3. Sort eligible IDs by `SHA-256("42:" + instance_id)` and take the first 30.
4. Save those 30 IDs in the run manifest before any model call.
5. Use one sample, temperature 0 where supported, batch size 1, one fixed model
   and one fixed Agent budget.
6. Execute generated code only through the official evaluator in an isolated
   environment; the upstream project recommends sandboxed/Docker evaluation.

Report Pass@1 plus median tokens, duration, model calls and tool calls. Do not
replace failed cases or change the selector after seeing results.

## SWE-bench Verified five-case pilot

Upstream: <https://github.com/SWE-bench/SWE-bench>

SWE-bench Verified contains 500 human-validated repository issues and uses a
containerized evaluation harness. A five-case run is labelled
**SWE-bench Verified 5-case pilot/subset**, never the full benchmark score.

Before running:

1. Pin the official dataset and harness commits and verify Docker capacity.
2. Sort all eligible Verified instance IDs by
   `SHA-256("42:" + instance_id)` and take the first five.
3. Save the IDs and Agent/model configuration before generation.
4. Give the Agent only the issue statement and the checked-out task repository;
   keep grader tests and gold patches unavailable.
5. Export the Agent patch, then run the official containerized evaluator.

Report resolved/5, per-instance status, tokens, duration, tool/model calls,
verification attempts, false completions and NO_PROGRESS stops. Environment or
image failures are reported separately and are not silently converted to task
failures or removed from the manifest.

## Local hidden and repeated evaluation

`run_eval.py` keeps visible regression tests in the Agent workspace but creates
the additional hidden grader only after the Agent exits, in a separate temporary
directory. The grader is deleted after evaluation. Repeated runs are supported:

```powershell
python .\evals\run_eval.py --dry-run
python .\evals\run_eval.py --case percentage-pricing --repeat 3
```

To run the recommended repeated pilot, execute the full eight cases once, then
run three predeclared representative cases with `--repeat 3`. Keep all reports;
do not select the best repetition.
