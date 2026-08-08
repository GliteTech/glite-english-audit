
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

INSERT INTO session VALUES ('ses_m01', 'proj_m', NULL, NULL, NULL, NULL, '1.2.0', NULL, 1767268800000, 1767268920000, NULL);
INSERT INTO message VALUES ('msg_m01', 'ses_m01', 1767268860000, 1767268860000, '{"role": "user", "time": {"created": 1767268860000}}');
INSERT INTO part VALUES ('prt_m01a', 'msg_m01', 'ses_m01', 1767268860000, 1767268860000, '{"text": "I have doubt about how to name this function, can you propose something?", "type": "text"}');
INSERT INTO message VALUES ('msg_m02', 'ses_m01', 1767268920000, 1767268920000, '{"role": "user", "time": {"created": 1767268920000}}');
