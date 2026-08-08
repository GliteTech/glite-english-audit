CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
INSERT INTO ItemTable(key, value) VALUES ('composer.composerData', '{"allComposers": [{"composerId": "11111111-aaaa-4aaa-8aaa-111111111111", "createdAt": 1746350000000, "lastUpdatedAt": 1746353100000, "name": "FAKE synthetic chat", "unifiedMode": "chat"}], "hasMigratedComposerData": true, "hasMigratedMultipleComposers": true, "selectedComposerIds": []}');
