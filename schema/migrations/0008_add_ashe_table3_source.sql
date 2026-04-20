-- 0008_add_ashe_table3_source.sql
-- Add source entry for ONS ASHE Table 3 (Region × 2-digit SOC)

INSERT INTO dim_source (source_id, display_name, provider, licence, cadence, confidence_weight, notes) VALUES
    ('ons_ashe_table3', 'ONS ASHE Table 3 (Region × Occupation)', 'ONS', 'OGL v3.0', 'annual', 0.95,
     'ASHE Table 3: earnings by region and 2-digit SOC 2020 code. Provides the critical occupation × region cross-dimension.')
ON CONFLICT (source_id) DO UPDATE SET
    display_name      = EXCLUDED.display_name,
    provider          = EXCLUDED.provider,
    licence           = EXCLUDED.licence,
    cadence           = EXCLUDED.cadence,
    confidence_weight = EXCLUDED.confidence_weight,
    notes             = EXCLUDED.notes;
