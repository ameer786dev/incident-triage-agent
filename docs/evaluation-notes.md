# Reading the first results

The retained initial stdio run scored 153/180 root-cause exact matches. Its rule
baseline, retrieval providers and test corpus were frozen before that run.
Subsequent changes address malformed tool payloads, model configuration,
citation source validation, and connection/query timeouts. They do not change
the diagnostic rules or tune retrieval scores against the test failures.

| Reference state | Correct / total |
|---|---:|
| Deployment regression | 12 / 16 |
| Database timeout | 16 / 16 |
| Memory exhaustion | 16 / 16 |
| CPU saturation | 16 / 16 |
| Exhausted connection pool | 16 / 16 |
| DNS failure | 16 / 16 |
| Rate limiting | 16 / 16 |
| Dependency timeout | 8 / 16 |
| Full disk | 16 / 16 |
| Recovered / healthy | 9 / 16 |
| Underdetermined / unknown | 12 / 20 |

The baseline abstained on 31 incidents, including 12 of the 20 expected unknown
cases. This gives 38.7% abstention precision and 60% recall. The overall 85%
accuracy therefore should not hide the ambiguity-handling weakness. The fixed
signature/threshold approach is intentionally inspectable but sensitive to
wording and incomplete observations.

Retrieval hit@3 was 114/160 (71.25%). Unknown cases do not have a reference
runbook and are excluded. Diagnosis uses its observations independently of the
retrieval ranking, so the root-cause and retrieval scores measure different
things. Improving runbook recall requires a new development experiment followed
by a fresh held-out measurement; it is not evidence that the first-run results
were higher than recorded.

All 353 cited IDs existed in the observed tool data and matched their claimed
service/source. This is a provenance check, not proof of causal correctness:
some wrong diagnoses can still cite genuine observations.

The p50/p95 latency of 11.110/13.860 ms uses a warm serial baseline process and
real local stdio MCP calls against frozen snapshots. No hosted LLM was invoked.
The $0 figure is model token charge, excluding CPU, storage, subscriptions,
networking and engineering time. No model accuracy or hosted latency/cost number
is available yet.

The post-review verification run retained the same 153 correct diagnoses and
114 runbook hits, with no rule or ranking-weight changes. That run overlapped
the separate retrieval-only ablation, so its timing is not used as the primary
performance result. Both complete run directories are kept.
