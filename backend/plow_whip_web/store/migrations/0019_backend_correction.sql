-- One-time legacy body scrub. Runtime writes persist only refs and metadata.
UPDATE host_jobs
SET result_json = json_remove(
    result_json,
    '$.stdout', '$.stderr', '$.prompt', '$.prompt_text',
    '$.execution.stdout', '$.execution.stderr',
    '$.execution.prompt', '$.execution.prompt_text'
)
WHERE result_json IS NOT NULL
  AND json_valid(result_json)
  AND (
      instr(result_json, '"stdout"') > 0
      OR instr(result_json, '"stderr"') > 0
      OR instr(result_json, '"prompt"') > 0
      OR instr(result_json, '"prompt_text"') > 0
  );

UPDATE task_runs
SET result_json = json_remove(
    result_json,
    '$.stdout', '$.stderr', '$.prompt', '$.prompt_text',
    '$.execution.stdout', '$.execution.stderr',
    '$.execution.prompt', '$.execution.prompt_text'
)
WHERE result_json IS NOT NULL
  AND json_valid(result_json)
  AND (
      instr(result_json, '"stdout"') > 0
      OR instr(result_json, '"stderr"') > 0
      OR instr(result_json, '"prompt"') > 0
      OR instr(result_json, '"prompt_text"') > 0
  );
