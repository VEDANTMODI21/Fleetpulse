# ADR-004: Gradient-boosted trees + explainable rules for breakdown prediction

**Status:** accepted

**Context.** The model must rank 100K vehicles daily by 7-day breakdown risk. Workshop capacity is about 1 % of the fleet
per day. The data is tabular rollups with NULL-heavy signals (EVs have no coolant, telemetry has gaps), and managers need
to see why a vehicle was flagged.

**Options.** Severe-DTC rule (industry default); static thresholds; logistic regression; gradient-boosted trees; sequence
deep learning (LSTM/Transformer); an LLM.

**Decision.** `HistGradientBoostingClassifier` on 49 windowed features (last, 3-day min/max, 7-day mean/slope,
self-baseline z-score). It handles NULLs natively and trains in 19 s on 0.8M rows. Inference takes 4 µs per vehicle.
Human-readable drivers come from ranked threshold deviations.

**Evidence** (temporal hold-out, 1.5M vehicle-days): PR-AUC 0.53 vs 0.28 for static thresholds and 0.12 for the DTC rule.
Precision at the 1 % budget is 0.57 vs 0.41 and 0.23. The model catches 87 % of breakdown events with a median 7 days of
lead time, while raising 6x fewer flags than "any threshold rule".

**Consequences.** Deep sequence models are deferred until real multi-OEM data exists. On synthetic data they would mostly
learn the simulator. The LLM is used only as an optional planner for the copilot, never for the risk score.
