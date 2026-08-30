# Benchmark results

This report records the published 2026-08-31 benchmark snapshot. Failed,
interrupted and budget-limited tasks remain in every denominator.

## Run identity

| Field | Value |
| --- | --- |
| Model | `deepseek-v4-flash` |
| Source snapshot SHA-256 | `412c184c9b728e8160d9b59d7666c50ae59bada1c17881e274fcf79409755f5c` |
| Python | `3.12.13` |
| Date | `2026-08-31` |

The source snapshot is the frozen release candidate used for generation. API
keys, machine-local paths, caches and generated workspaces are not part of this
report.

## Summary

| Benchmark | Result | Tokens | Median tokens/task | Median duration/task |
| --- | ---: | ---: | ---: | ---: |
| Local hidden repair suite | **7/8 (87.5%)** | 177,031 | 19,727.5 | 8.469 s |
| BigCodeBench Instruct 30-case pilot | **12/30 (40.0%)** | 795,374 | 23,139 | 16.328 s |

The local run used 43 model calls, 52 tool calls and 9 verification attempts
over 80.173 seconds. BigCodeBench generation used 148 model calls and 159 tool
calls over 1,005.188 seconds.

## Local hidden repair suite

Each task starts from a failing visible test suite. The Agent can inspect those
tests, but the additional grader is materialized in a separate directory only
after the Agent exits. The harness also verifies that visible tests were not
modified.

| Task | Hidden result | False completion | Tokens | Duration |
| --- | ---: | ---: | ---: | ---: |
| `percentage-pricing` | pass | no | 39,356 | 18.875 s |
| `half-open-intervals` | pass | no | 19,915 | 9.438 s |
| `inventory-boundary` | pass | no | 19,327 | 7.844 s |
| `config-none-merge` | pass | no | 19,241 | 7.469 s |
| `pagination-offset` | pass | no | 19,059 | 7.422 s |
| `cache-expiration` | pass | no | 19,927 | 9.094 s |
| `slug-normalization` | **fail** | **yes** | 20,666 | 12.593 s |
| `retry-schedule` | pass | no | 19,540 | 7.438 s |

`slug-normalization` passed the visible verifier but failed the hidden
underscore-boundary case. It is recorded as a false completion rather than a
successful task.

## BigCodeBench Instruct 30-case pilot

This is a deterministic Agent pilot, not a full BigCodeBench leaderboard score.

- Upstream source commit:
  `09dd993f46c3fbf3a799465bb96d524edcb0b199`
- Dataset: `bigcode/bigcodebench`, release `v0.1.4`, 1,140 rows
- Dataset content SHA-256:
  `053fcfcb880b60b0183554cc756a6530254699f18903820b5b9e559436198e26`
- Eligibility: 327 standard-library-only tasks
- Selection: sort eligible IDs by `SHA-256("42:" + task_id)`, take the first 30
- Limits per task: 12 Agent steps, 300 seconds and 30,000 task tokens
- Evaluation: official remote BigCodeBench evaluator, calibrated mode

The Runtime completed 21 tasks and stopped 9 at the fixed token budget. Official
evaluation passed 12 tasks: 10 Runtime-completed outputs and 2 outputs from
budget-limited runs. All 30 tasks remain in Pass@1.

<details>
<summary>Per-task results</summary>

| Task | Official | Runtime | Termination | Tokens |
| --- | ---: | ---: | --- | ---: |
| `BigCodeBench/0` | pass | completed | completed | 20,847 |
| `BigCodeBench/13` | fail | completed | completed | 18,572 |
| `BigCodeBench/15` | fail | incomplete | max task tokens | 42,070 |
| `BigCodeBench/113` | pass | completed | completed | 23,606 |
| `BigCodeBench/256` | fail | incomplete | max task tokens | 34,098 |
| `BigCodeBench/260` | pass | completed | completed | 22,443 |
| `BigCodeBench/283` | pass | completed | completed | 15,693 |
| `BigCodeBench/288` | fail | incomplete | max task tokens | 32,434 |
| `BigCodeBench/320` | fail | completed | completed | 19,089 |
| `BigCodeBench/400` | fail | completed | completed | 21,702 |
| `BigCodeBench/547` | pass | completed | completed | 33,125 |
| `BigCodeBench/565` | fail | completed | completed | 34,878 |
| `BigCodeBench/592` | pass | incomplete | max task tokens | 34,008 |
| `BigCodeBench/594` | fail | incomplete | max task tokens | 30,753 |
| `BigCodeBench/708` | fail | completed | completed | 21,202 |
| `BigCodeBench/716` | fail | completed | completed | 22,662 |
| `BigCodeBench/740` | fail | completed | completed | 30,445 |
| `BigCodeBench/775` | fail | completed | completed | 39,206 |
| `BigCodeBench/777` | fail | completed | completed | 17,125 |
| `BigCodeBench/830` | pass | completed | completed | 19,120 |
| `BigCodeBench/831` | fail | incomplete | max task tokens | 34,966 |
| `BigCodeBench/847` | fail | completed | completed | 26,403 |
| `BigCodeBench/853` | fail | incomplete | max task tokens | 37,314 |
| `BigCodeBench/861` | pass | incomplete | max task tokens | 30,270 |
| `BigCodeBench/862` | pass | completed | completed | 22,672 |
| `BigCodeBench/971` | pass | completed | completed | 21,588 |
| `BigCodeBench/998` | fail | incomplete | max task tokens | 34,322 |
| `BigCodeBench/1027` | pass | completed | completed | 20,528 |
| `BigCodeBench/1050` | pass | completed | completed | 15,360 |
| `BigCodeBench/1130` | fail | completed | completed | 18,873 |

</details>

## Reading the result

BigCodeBench kept its official tests outside the Agent workspace, so the local
Runtime could establish syntax, workspace freshness and fingerprint binding but
could not establish hidden semantic correctness. The gap between 21 Runtime
completions and 12 official passes is therefore an adequacy gap, not a workspace
freshness failure. A project-selected final verifier remains the strongest local
completion oracle.
