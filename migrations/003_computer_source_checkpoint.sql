ALTER TABLE computers ADD COLUMN source_checkpoint_id TEXT REFERENCES checkpoints(id);
