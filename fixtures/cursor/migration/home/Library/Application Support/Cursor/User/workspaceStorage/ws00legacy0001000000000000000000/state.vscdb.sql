CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
INSERT INTO ItemTable(key, value) VALUES ('workbench.panel.aichat.view.aichat.chatdata', '{"tabs": [{"bubbles": [{"text": "SYNTHETIC g1 bubble", "type": "user"}], "tabId": "FAKE-tab-1"}]}');
INSERT INTO ItemTable(key, value) VALUES ('composer.composerData', '{"allComposers": [{"composerId": "dddd6666-0000-4000-8000-000000000006", "conversation": [{"text": "SYNTHETIC g2 inline conversation", "type": 1}]}], "hasMigratedComposerData": true}');
