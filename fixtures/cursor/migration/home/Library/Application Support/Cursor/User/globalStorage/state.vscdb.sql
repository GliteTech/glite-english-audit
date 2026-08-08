CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
INSERT INTO cursorDiskKV(key, value) VALUES ('composerData:cccc3333-0000-4000-8000-000000000003', '{"_v": 2, "composerId": "cccc3333-0000-4000-8000-000000000003", "conversationMap": {"FAKE-bubble-1": {"text": "SYNTHETIC legacy embedded message", "type": 1}}, "createdAt": 1704067200000, "fullConversationHeadersOnly": []}');
