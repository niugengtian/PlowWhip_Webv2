UPDATE provider_configs
SET capabilities_json = '["new_session","resume_session","refine_convention"]'
WHERE name IN ('codex', 'cursor');
