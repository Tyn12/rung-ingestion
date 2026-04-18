-- Add the Reed-ratio-shaped ASHE source.
-- These are synthetic experience-band observations derived by applying
-- junior/mid/senior salary ratios from Reed listings to ASHE percentiles.
-- Confidence is 0.80 (lower than raw ASHE 0.95) to reflect the assumption layer.

INSERT INTO dim_source (source_id, display_name, provider, licence, cadence, confidence_weight, notes) VALUES
    ('ons_ashe_table2_shaped', 'ONS ASHE Table 2 (shaped by Reed experience ratios)', 'ONS + Reed', 'OGL v3.0 + Reed T&Cs', 'annual', 0.80,
     'ASHE occupation percentiles scaled by experience-band ratios derived from Reed job listings. Provides junior/mid/senior breakdowns that raw ASHE lacks.')
ON CONFLICT (source_id) DO UPDATE
SET display_name      = EXCLUDED.display_name,
    provider          = EXCLUDED.provider,
    licence           = EXCLUDED.licence,
    cadence           = EXCLUDED.cadence,
    confidence_weight = EXCLUDED.confidence_weight,
    notes             = EXCLUDED.notes;
