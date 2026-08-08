
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


CREATE TABLE account (id TEXT PRIMARY KEY, provider TEXT, access_token TEXT, refresh_token TEXT);
CREATE TABLE credential (id TEXT PRIMARY KEY, value TEXT);
CREATE TABLE session_share (id TEXT PRIMARY KEY, session_id TEXT, secret TEXT, url TEXT);
CREATE TABLE event (id INTEGER PRIMARY KEY, type TEXT, payload TEXT);
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT);

