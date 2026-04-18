-- Add ONS ASHE Table 2 as a data source.
-- This is the occupation-level pay data from the Annual Survey of Hours and Earnings,
-- published by ONS as downloadable Excel workbooks (Table 2 series).
-- Higher confidence than Nomis ASHE because it has occupation-level breakdowns.

INSERT INTO dim_source (source_id, display_name, provider, licence, cadence, confidence_weight, notes) VALUES
    ('ons_ashe_table2', 'ONS ASHE Table 2 (Occupation, SOC 2020)', 'ONS', 'OGL v3.0', 'annual', 0.95,
     'Official occupation-level pay by SOC 2020 from ASHE Table 2 Excel workbooks. National (UK) data, full-time employees. Includes percentiles 10/20/25/30/40/50(median)/60/70/75/80/90 and mean.')
ON CONFLICT (source_id) DO UPDATE
SET display_name      = EXCLUDED.display_name,
    provider          = EXCLUDED.provider,
    licence           = EXCLUDED.licence,
    cadence           = EXCLUDED.cadence,
    confidence_weight = EXCLUDED.confidence_weight,
    notes             = EXCLUDED.notes;
