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


CREATE TABLE account (id TEXT PRIMARY KEY, provider TEXT, access_token TEXT, refresh_token TEXT);
CREATE TABLE credential (id TEXT PRIMARY KEY, value TEXT);
CREATE TABLE session_share (id TEXT PRIMARY KEY, session_id TEXT, secret TEXT, url TEXT);
CREATE TABLE event (id INTEGER PRIMARY KEY, type TEXT, payload TEXT);
CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT);

INSERT INTO account VALUES ('acc_01', 'anthropic', 'sk-ant-FAKEFAKEFAKE0000', 'rt-FAKEFAKEFAKE0000');
INSERT INTO credential VALUES ('cred_01', 'token-FAKEFAKEFAKE0000');
INSERT INTO session_share VALUES ('shr_01', 'ses_alpha01', 'share-secret-FAKEFAKEFAKE0000', 'https://example.invalid/s/FAKE');
INSERT INTO event VALUES (1, 'message.updated', '{"note": "synthetic event payload FAKE"}');
INSERT INTO project VALUES ('proj_alpha', '/home/synthetic/FAKE-alpha');
INSERT INTO project VALUES ('proj_beta', '/home/synthetic/FAKE-beta');
INSERT INTO session VALUES ('ses_alpha01', 'proj_alpha', NULL, NULL, NULL, NULL, '1.18.2', NULL, 1780308000000, 1780308420000, NULL);
INSERT INTO session VALUES ('ses_alpha02', 'proj_alpha', NULL, NULL, NULL, NULL, '1.18.2', NULL, 1780311600000, 1780311660000, 1780315200000);
INSERT INTO session VALUES ('ses_alpha03', 'proj_alpha', 'ses_alpha01', NULL, NULL, NULL, '1.18.2', NULL, 1780308500000, 1780308560000, NULL);
INSERT INTO session VALUES ('ses_beta01', 'proj_beta', NULL, NULL, NULL, NULL, '1.18.2', NULL, 1782896400000, 1782896640000, NULL);
INSERT INTO session VALUES ('ses_beta02', 'proj_beta', NULL, NULL, NULL, NULL, '1.18.2', NULL, 1782900000000, 1782900120000, NULL);
INSERT INTO message VALUES ('msg_a01', 'ses_alpha01', 1780308060000, 1780308060000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308060000}}');
INSERT INTO part VALUES ('prt_a01a', 'msg_a01', 'ses_alpha01', 1780308060000, 1780308060000, '{"text": "I very like this plan, can we start from the login page?", "type": "text"}');
INSERT INTO message VALUES ('msg_a02', 'ses_alpha01', 1780308120000, 1780308120000, '{"cost": 0.01, "role": "assistant", "time": {"created": 1780308120000}}');
INSERT INTO part VALUES ('prt_a02a', 'msg_a02', 'ses_alpha01', 1780308120000, 1780308120000, '{"text": "Sure, here is the outline of the login page.", "type": "text"}');
INSERT INTO message VALUES ('msg_a03', 'ses_alpha01', 1780308180000, 1780308180000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308180000}}');
INSERT INTO part VALUES ('prt_a03a', 'msg_a03', 'ses_alpha01', 1780308180000, 1780308180000, '{"text": "Here is my plan for the next sprint.", "type": "text"}');
INSERT INTO part VALUES ('prt_a03b', 'msg_a03', 'ses_alpha01', 1780308180000, 1780308180000, '{"text": "Please review it and tell me if something is wrong there.", "type": "text"}');
INSERT INTO message VALUES ('msg_a04', 'ses_alpha01', 1780308240000, 1780308240000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308240000}}');
INSERT INTO part VALUES ('prt_a04a', 'msg_a04', 'ses_alpha01', 1780308240000, 1780308240000, '{"synthetic": true, "text": "Read tool was called for file review.", "type": "text"}');
INSERT INTO part VALUES ('prt_a04b', 'msg_a04', 'ses_alpha01', 1780308240000, 1780308240000, '{"text": "Also the tests are still red, why it can be?", "type": "text"}');
INSERT INTO message VALUES ('msg_a05', 'ses_alpha01', 1780308300000, 1780308300000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308300000}}');
INSERT INTO part VALUES ('prt_a05a', 'msg_a05', 'ses_alpha01', 1780308300000, 1780308300000, '{"ignored": true, "text": "This draft part was ignored by the user.", "type": "text"}');
INSERT INTO message VALUES ('msg_a06', 'ses_alpha01', 1780308360000, 1780308360000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308360000}}');
INSERT INTO part VALUES ('prt_a06a', 'msg_a06', 'ses_alpha01', 1780308360000, 1780308360000, '{"text": "synthetic reasoning FAKE", "type": "reasoning"}');
INSERT INTO part VALUES ('prt_a06b', 'msg_a06', 'ses_alpha01', 1780308360000, 1780308360000, '{"text": "Please explain me how the cache invalidation works here.", "type": "text"}');
INSERT INTO message VALUES ('msg_a07', 'ses_alpha01', 1780308420000, 1780308420000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308420000}}');
INSERT INTO part VALUES ('prt_a07a', 'msg_a07', 'ses_alpha01', 1780308420000, 1780308420000, '{"text": "Please analyze this codebase and create an AGENTS.md file containing:\n1. Build/lint/test commands - especially for running a single test\n2. Code style guidelines including imports, formatting, types, naming conventions, etc.", "type": "text"}');
INSERT INTO message VALUES ('msg_a08', 'ses_alpha02', 1780311660000, 1780311660000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780311660000}}');
INSERT INTO part VALUES ('prt_a08a', 'msg_a08', 'ses_alpha02', 1780311660000, 1780311660000, '{"text": "This task is finish, I will close the session now.", "type": "text"}');
INSERT INTO message VALUES ('msg_a09', 'ses_alpha03', 1780308560000, 1780308560000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1780308560000}}');
INSERT INTO part VALUES ('prt_a09a', 'msg_a09', 'ses_alpha03', 1780308560000, 1780308560000, '{"text": "Summarize the repository structure for the subtask.", "type": "text"}');
INSERT INTO message VALUES ('msg_b01', 'ses_beta01', 1782896460000, 1782896460000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1782896460000}}');
INSERT INTO part VALUES ('prt_b01a', 'msg_b01', 'ses_beta01', 1782896460000, 1782896460000, '{"text": "Today I written a short note about my English learning progress.", "type": "text"}');
INSERT INTO message VALUES ('msg_b02', 'ses_beta01', 1782896520000, 1782896520000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1782896520000}}');
INSERT INTO part VALUES ('prt_b02a', 'msg_b02', 'ses_beta01', 1782896520000, 1782896520000, '{"text": "Can you help me to make this sentence more natural? It sounds not good for me.", "type": "text"}');
INSERT INTO message VALUES ('msg_b03', 'ses_beta01', 1782896580000, 1782896580000, '{"cost": 0.01, "role": "assistant", "time": {"created": 1782896580000}}');
INSERT INTO part VALUES ('prt_b03a', 'msg_b03', 'ses_beta01', 1782896580000, 1782896580000, '{"text": "Here is a more natural version of the sentence.", "type": "text"}');
INSERT INTO message VALUES ('msg_b04', 'ses_beta01', 1782896640000, 1782896640000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1782896640000}}');
INSERT INTO part VALUES ('prt_b04a', 'msg_b04', 'ses_beta01', 1782896640000, 1782896640000, '{"filename": "notes.txt", "type": "file", "url": "file:///FAKE/notes.txt"}');
INSERT INTO part VALUES ('prt_b04b', 'msg_b04', 'ses_beta01', 1782896640000, 1782896640000, '{"text": "I am agree with your suggestion, let us do it so.", "type": "text"}');
INSERT INTO message VALUES ('msg_b05', 'ses_beta02', 1782900060000, 1782900060000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1782900060000}}');
INSERT INTO part VALUES ('prt_b05a', 'msg_b05', 'ses_beta02', 1782900060000, 1782900060000, '{"text": "Since many years I want to speak English more fluent.", "type": "text"}');
INSERT INTO message VALUES ('msg_b06', 'ses_beta02', 1782900120000, 1782900120000, '{"agent": "build", "model": {"modelID": "claude-sonnet-4-5", "providerID": "anthropic"}, "role": "user", "time": {"created": 1782900120000}}');
INSERT INTO part VALUES ('prt_b06a', 'msg_b06', 'ses_beta02', 1782900120000, 1782900120000, '{"text": "How I can practice the passive voice more often?", "type": "text"}');
