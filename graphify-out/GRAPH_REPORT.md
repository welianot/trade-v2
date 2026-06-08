# Graph Report - .  (2026-06-02)

## Corpus Check
- 3 files · ~0 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 9 nodes · 8 edges · 3 communities (1 shown, 2 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 1 edges (avg confidence: 0.85)
- Token cost: 0 input · 1,256 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Strategy Detection|Strategy Detection]]
- [[_COMMUNITY_Trade Execution|Trade Execution]]
- [[_COMMUNITY_Risk Management|Risk Management]]

## God Nodes (most connected - your core abstractions)
1. `detect_liquidity_grabs()` - 3 edges
2. `SMC Strategy` - 3 edges
3. `run_backtest()` - 2 edges
4. `place_bracket()` - 2 edges
5. `Risk Management` - 2 edges

## Surprising Connections (you probably didn't know these)
- `place_bracket()` --implements--> `Risk Management`  [EXTRACTED]
  live_trade.py → back_test.py

## Import Cycles
- None detected.

## Communities (3 total, 2 thin omitted)

### Community 0 - "Strategy Detection"
Cohesion: 0.50
Nodes (3): run_backtest(), place_bracket(), Risk Management

## Knowledge Gaps
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.