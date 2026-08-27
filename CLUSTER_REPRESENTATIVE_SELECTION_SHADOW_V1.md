# Cluster Representative Selection Shadow V1

Status: Implemented as analytics-only tooling  
Scope: Production Season 1 research  
Live enforcement: Not implemented  

## Problem Statement

Production correlation filtering reduces same-direction cluster exposure. Season 1 shadow data suggests the correlation gate itself should remain live unchanged, but a separate research question remains:

Could the scanner sometimes be selecting the wrong representative from a correlated cluster?

This shadow tool evaluates representative-selection strategies on correlation-rejected candidates only.

## Motivation

Prior rejected outcome shadow audit found:

- Full rejected population: 7,116 candidates
- Correlation rejected: 657 candidates
- Correlation rejected hypothetical performance: about 49.9% WR and +51R
- One representative per UTC hour + side produced about 122 clusters
- Best score / best confidence / score+confidence strategies were near positive hypothetical Net R

These figures are motivation only. They are not hard-coded into the tool.

## Safety

This module is report-only.

It does not:

- Change live correlation filtering
- Change scanner logic
- Change score or confidence
- Change signal approval
- Change Telegram/Cornix routing
- Change TP/SL/RR
- Change outcome review
- Change production universe

`CLUSTER_REPRESENTATIVE_LIVE_ENABLED` is ignored in V1 if present.

## Inputs

Default files:

- `logs/rejected_outcome_shadow.csv`
- `logs/signals.csv`

The rejected outcome file must already contain hypothetical outcomes from Rejected Candidate Outcome Shadow V1.

## Population

V1 uses only candidates where:

- `rejection_reason == CORRELATION`

It does not mix:

- `QUALITY`
- `LOSS_COOLDOWN`
- `DAILY_RISK_GUARD`
- `BTC_REGIME`
- `NOT_TOP`
- `OTHER`

## Cluster Definition

Conservative V1 cluster:

- Same UTC 1H candle/hour
- Same side: `LONG` or `SHORT`
- Correlation-rejected population only

Cluster key:

```text
cluster:v1:<YYYY-MM-DDTHH:00:00Z>|<SIDE>|<12-char hash>
```

## Canonical Identity

Each representative keeps:

- `representative_canonical_key`

This uses the same canonical signal identity helpers as the existing shadow linkage system.

## Representative Strategies

All ranking strategies use stored candidate fields only. Outcome data is joined after the representative is selected.

### BEST_SCORE

Sort by:

1. score descending
2. confidence descending
3. tier priority descending
4. deterministic tie key ascending

### BEST_CONFIDENCE

Sort by:

1. confidence descending
2. score descending
3. tier priority descending
4. deterministic tie key ascending

### SCORE_PLUS_CONFIDENCE

Sort by:

1. score + confidence descending
2. score descending
3. confidence descending
4. tier priority descending
5. deterministic tie key ascending

### TIER_SCORE_CONF

Tier priority:

- A = 3
- B = 2
- C = 1
- Unknown = 0

Sort by:

1. tier priority descending
2. score descending
3. confidence descending
4. deterministic tie key ascending

### QUALITY_COMPOSITE_V1

Transparent analytics-only formula:

```text
0.45 * score
+ 0.35 * confidence
+ 0.20 * tier_quality_points
```

Tier quality points:

- A = 100
- B = 70
- C = 40
- Unknown = 50

No outcome, future candle, or post-trade data participates in this ranking.

## Production Comparison

For each rejected cluster, the tool searches production `sent` signals in the same:

- UTC hour
- side

It records:

- whether production sent exists
- production sent count
- all sent symbols
- deterministic comparison representative
- production comparison outcome/R when available

If multiple production sent rows exist, multiplicity is preserved in `production_sent_count`. A deterministic comparison row is selected only for aggregate analytics.

## Output

Output file:

- `logs/cluster_representative_shadow.csv`

Fields:

- `cluster_key`
- `cluster_hour_utc`
- `side`
- `cluster_size`
- `candidate_symbols`
- `strategy`
- `representative_canonical_key`
- `representative_symbol`
- `representative_score`
- `representative_confidence`
- `representative_tier`
- `hypothetical_outcome`
- `hypothetical_r`
- `production_sent_count`
- `production_sent_present`
- `production_sent_symbols`
- `production_comparison_symbol`
- `production_comparison_score`
- `production_comparison_confidence`
- `production_comparison_tier`
- `production_comparison_outcome`
- `production_comparison_r`
- `shadow_vs_production_r_delta`
- `source_population`
- `generated_at_utc`

The logger is append-only and dedupes by:

```text
cluster_key + strategy
```

## CLI

```bash
python -m core.cluster_representative_shadow --dry-run
python -m core.cluster_representative_shadow --limit 20
python -m core.cluster_representative_shadow --limit 100
python -m core.cluster_representative_shadow
```

Dry run prints summary only and does not write CSV.

## Bias Controls

- Outcome data is not used for representative selection.
- Hypothetical outcome is joined only after ranking is finalized.
- Ranking uses fields available at candidate creation time: score, confidence, tier, symbol, entry, timestamp.
- The population is limited to correlation-rejected candidates only.
- Production comparison is separated from rejected representative performance.
- No live recommendation is generated from a single run.

## Summary Analytics

CLI prints:

- clusters evaluated
- mean cluster size
- max cluster size
- strategy-level N / resolved / WR / hypothetical Net R
- production sent linkage coverage
- comparable shadow-production R delta
- monthly stability by strategy

## Historical Audit Expectations

Prior audit ballpark:

- Correlation rejected: about 657
- Hour + side clusters: about 122
- Mean cluster size: about 5.39
- Max cluster size: about 24
- BEST_SCORE: about +17R
- BEST_CONFIDENCE: about +15R
- SCORE_PLUS_CONFIDENCE: about +18R

These values are sanity references only and must not be hard-coded.

## Limitations

- Correlation clusters are approximated by UTC hour + side, not the exact internal live cluster object.
- Production sent comparison can have zero, one, or multiple rows.
- Hypothetical outcomes depend on `rejected_outcome_shadow.csv` being populated.
- The tool does not prove an unselected candidate would have been executed by a human.
- Slippage, spread, funding, missed fills, and exchange execution are not modeled.

## Rollout Plan

Phase 1:

- Run dry-run on production data.
- Confirm cluster counts and strategy summaries.

Phase 2:

- Write `logs/cluster_representative_shadow.csv`.
- Keep live correlation gate unchanged.

Phase 3:

- Add report section after enough stable samples.
- Compare representative strategies by month and side.

Phase 4:

- Only consider live experiments after repeated stable evidence and separate approval.

## Recommendation

Keep this in shadow mode. Correlation live gate remains unchanged.
