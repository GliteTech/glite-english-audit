
CREATE TABLE session (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  parent_id TEXT,
  version TEXT,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL
);
CREATE TABLE message (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  data TEXT NOT NULL
);

INSERT INTO session VALUES ('ses_u01', 'proj_u', NULL, '2.0.0', 1790000000000, 1790000000000);
INSERT INTO message VALUES ('msg_u01', 'ses_u01', 1790000060000, 1790000060000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1790000060000}}');
