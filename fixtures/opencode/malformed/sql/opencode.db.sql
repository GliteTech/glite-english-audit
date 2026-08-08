
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

INSERT INTO session VALUES ('ses_db01', 'proj_db', NULL, NULL, NULL, NULL, '1.18.0', NULL, 1775037600000, 1775037720000, NULL);
INSERT INTO message VALUES ('msg_db01', 'ses_db01', 1775037660000, 1775037660000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1775037660000}}');
INSERT INTO part VALUES ('prt_db01a', 'msg_db01', 'ses_db01', 1775037660000, 1775037660000, '{"text": "The linter complain about one unused import, should I remove it?", "type": "text"}');
INSERT INTO message VALUES ('msg_db02', 'ses_db01', 1775037720000, 1775037720000, 'not-json{');
