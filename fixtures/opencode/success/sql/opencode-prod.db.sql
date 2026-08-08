PRAGMA journal_mode=WAL;

CREATE TABLE session (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  parent_id TEXT,
  slug TEXT,
  directory TEXT,
  title TEXT,
  version TEXT,
  share_url TEXT,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  time_archived INTEGER
);
CREATE TABLE message (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE part (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  data TEXT NOT NULL
);

INSERT INTO session VALUES ('ses_alpha01', 'proj_alpha', NULL, NULL, NULL, NULL, '1.18.2', NULL, 1780308000000, 1780308420000, NULL);
INSERT INTO message VALUES ('msg_a01', 'ses_alpha01', 1780308060000, 1780308060000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308060000}}');
INSERT INTO part VALUES ('prt_a01a', 'msg_a01', 'ses_alpha01', 1780308060000, 1780308060000, '{"text": "I very like this plan, can we start from the login page?", "type": "text"}');
