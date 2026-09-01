# Benchmark results

This report records the published 2026-09-02 benchmark snapshot. Failed,
interrupted and budget-limited tasks remain in every denominator.

## Run identity

| Field | Value |
| --- | --- |
| Agent commit | `7d70a89da056b3492495a2bc834939fb955af370` |
| Frozen source SHA-256 | `0f44dc2ab54cc5d820b1b5150c6190ca4f6dbb2a9ef59937ee09c48256ce0af4` |
| Model | `deepseek-v4-flash` |
| Python | `3.12.13` |
| Date | `2026-09-02` |

The frozen source passed 186 tests and 9 subtests before evaluation. API keys,
machine-local paths, caches, generated workspaces and trajectories are not part
of this report.

## Summary

| Benchmark | Result | Tokens | Median tokens/task | Median duration/task |
| --- | ---: | ---: | ---: | ---: |
| Local hidden repair suite | **8/8 (100%)** | 183,866 | 21,645.5 | 9.688 s |
| BigCodeBench Instruct 30-case pilot | **15/30 (50.0%)** | 749,186 | 22,691 | 15.218 s |

The local run used 42 model calls, 50 tool calls and 10 verification attempts
over 78.968 seconds. Seven tasks passed their first check; the remaining task
recovered from a failed verification. There were no false completions or
`NO_PROGRESS` stops.

BigCodeBench generation used 139 model calls and 142 tool calls over 688.297
seconds. The provider reported 673,533 input tokens, 75,653 output tokens,
65,024 cache-hit tokens, 608,509 cache-miss tokens and 57,030 reasoning tokens.

### Comparison with the previous snapshot

| Metric | 2026-08-31 | 2026-09-02 |
| --- | ---: | ---: |
| Local hidden pass | 7/8 | **8/8** |
| Local false completions | 1 | **0** |
| BigCodeBench Pass@1 | 12/30 | **15/30** |
| BigCodeBench Runtime completions | 21/30 | **29/30** |
| BigCodeBench tokens | 795,374 | **749,186** |
| BigCodeBench duration | 1,005.188 s | **688.297 s** |

The comparison uses the same local fixtures and the same frozen BigCodeBench
manifest, model and per-task limits. It is a version comparison, not a claim
that a 30-task pilot represents the complete benchmark.

## Local hidden repair suite

Each task starts from a failing visible test suite. The Agent can inspect those
tests, but the additional grader is materialized in a separate directory only
after the Agent exits. The harness also verifies that visible tests were not
modified.

| Task | Hidden result | False completion | Tokens | Duration |
| --- | ---: | ---: | ---: | ---: |
| `percentage-pricing` | pass | no | 28,341 | 14.016 s |
| `half-open-intervals` | pass | no | 21,124 | 8.594 s |
| `inventory-boundary` | pass | no | 21,697 | 10.062 s |
| `config-none-merge` | pass | no | 21,360 | 8.062 s |
| `pagination-offset` | pass | no | 20,889 | 7.656 s |
| `cache-expiration` | pass | no | 21,975 | 9.516 s |
| `slug-normalization` | pass | no | 21,594 | 9.859 s |
| `retry-schedule` | pass | no | 26,886 | 11.203 s |

## BigCodeBench Instruct 30-case pilot

This is a deterministic Agent pilot, not a full BigCodeBench leaderboard score.

- Upstream source commit:
  `09dd993f46c3fbf3a799465bb96d524edcb0b199`
- Dataset: `bigcode/bigcodebench`, release `v0.1.4`, 1,140 rows
- Dataset content SHA-256:
  `053fcfcb880b60b0183554cc756a6530254699f18903820b5b9e559436198e26`
- Eligibility: 327 standard-library-only tasks
- Selection: sort eligible IDs by `SHA-256("42:" + task_id)`, take the first 30
- Manifest SHA-256:
  `a60a11d672df712dfc00b866eca894b001dec0566d55aa08907d744dd0781886`
- Limits per task: 12 Agent steps, 300 seconds and 30,000 task tokens
- Evaluation: official remote BigCodeBench evaluator, calibrated mode

The Runtime completed and final-verified 29 tasks; `BigCodeBench/256` stopped at
the fixed token budget. Official evaluation passed 15 tasks. All 30 tasks remain
in Pass@1.

<details>
<summary>Per-task results</summary>

| Task | Official | Runtime | Termination | Tokens |
| --- | ---: | ---: | --- | ---: |
| `BigCodeBench/0` | pass | completed | completed | 23,227 |
| `BigCodeBench/13` | fail | completed | completed | 19,381 |
| `BigCodeBench/15` | fail | completed | completed | 41,115 |
| `BigCodeBench/113` | pass | completed | completed | 26,596 |
| `BigCodeBench/256` | fail | incomplete | max task tokens | 33,705 |
| `BigCodeBench/260` | pass | completed | completed | 18,015 |
| `BigCodeBench/283` | pass | completed | completed | 21,658 |
| `BigCodeBench/288` | fail | completed | completed | 22,123 |
| `BigCodeBench/320` | fail | completed | completed | 40,036 |
| `BigCodeBench/400` | fail | completed | completed | 21,192 |
| `BigCodeBench/547` | pass | completed | completed | 16,979 |
| `BigCodeBench/565` | fail | completed | completed | 29,781 |
| `BigCodeBench/592` | pass | completed | completed | 31,525 |
| `BigCodeBench/594` | fail | completed | completed | 18,641 |
| `BigCodeBench/708` | fail | completed | completed | 19,459 |
| `BigCodeBench/716` | fail | completed | completed | 18,957 |
| `BigCodeBench/740` | pass | completed | completed | 21,837 |
| `BigCodeBench/775` | fail | completed | completed | 19,633 |
| `BigCodeBench/777` | fail | completed | completed | 23,466 |
| `BigCodeBench/830` | pass | completed | completed | 22,135 |
| `BigCodeBench/831` | fail | completed | completed | 28,905 |
| `BigCodeBench/847` | fail | completed | completed | 25,463 |
| `BigCodeBench/853` | pass | completed | completed | 33,702 |
| `BigCodeBench/861` | pass | completed | completed | 22,155 |
| `BigCodeBench/862` | pass | completed | completed | 30,205 |
| `BigCodeBench/971` | pass | completed | completed | 27,418 |
| `BigCodeBench/998` | fail | completed | completed | 26,040 |
| `BigCodeBench/1027` | pass | completed | completed | 21,759 |
| `BigCodeBench/1050` | pass | completed | completed | 23,481 |
| `BigCodeBench/1130` | pass | completed | completed | 20,597 |

</details>

The generation report, samples and official evaluator response were retained
outside the repository. Their SHA-256 digests are:

| Artifact | SHA-256 |
| --- | --- |
| Local hidden report | `95c89f80acdfc5fcae12ce23fb81b2f332a2f552a6d24b77d520c98ba67381c1` |
| Generation report | `2896cd465f9b99ce67aab0da6622e9d7c77d24a1df09b184b020837f5de049fb` |
| Samples | `138afb68e1721a58d466626cfa0abd191bc139805492b10ef698184a3023e350` |
| Official evaluation | `07dd2730daef47ad36377301a6e1547c43450cf6b8a3e8ef4f6e935fd6b1f697` |

## Reading the result

BigCodeBench kept its official tests outside the Agent workspace, so the local
Runtime could establish syntax, workspace freshness and fingerprint binding but
could not establish hidden semantic correctness. The gap between 29 Runtime
completions and 15 official passes is therefore an adequacy gap, not a workspace
freshness failure. A project-selected final verifier remains the strongest local
completion oracle.
