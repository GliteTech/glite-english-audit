
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

CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
CREATE TABLE session_input (id TEXT PRIMARY KEY, session_id TEXT, prompt TEXT);
INSERT INTO session VALUES ('ses_v01', 'proj_v', NULL, NULL, NULL, NULL, '2.1.0', NULL, 1790000000000, 1790000000000, NULL);
INSERT INTO session VALUES ('ses_v02', 'proj_v', NULL, NULL, NULL, NULL, '2.1.0', NULL, 1790000100000, 1790000100000, NULL);
INSERT INTO session_message VALUES ('smg_01', 'ses_v01', '{"note": "synthetic v2 payload FAKE"}');
INSERT INTO session_input VALUES ('sin_01', 'ses_v01', 'synthetic v2 prompt FAKE');
