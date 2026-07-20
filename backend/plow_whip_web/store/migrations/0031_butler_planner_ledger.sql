ALTER TABLE model_calls ADD COLUMN proposal_revision INTEGER
    CHECK (proposal_revision IS NULL OR proposal_revision >= 0);
ALTER TABLE model_calls ADD COLUMN raw_status TEXT;
