"""Dashboard analytics pre-computation layer.

This package computes the full dashboard JSONB payload for each
(occupation, location, sector, experience_band) combination, using only
data from compensation_observations, compensation_aggregates, and
reference dimensions.  No LLM, no hand-tuning — every number is a
deterministic function of the dataset.
"""
