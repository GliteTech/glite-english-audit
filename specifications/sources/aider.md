# Source specification: Aider (`aider`)

Status: reviewed source specification for the `aider` adapter (spec section 4.2 research gate).
Adapter ID: `aider`. Stability target: stable (after release gates pass).
Research log: `temp/findings/aider-source-research.md` (evidence IDs E1–E14 cited below).
Access date for all cited evidence: 2026-08-08.

Research basis: primary vendor source code (`Aider-AI/aider` on GitHub, `main` branch) and the
`prompt_toolkit` library source that defines the input-history serialization, plus vendor
documentation and release notes. Aider is not installed on the reference machine; no local
installation was inspected and no independent reverse engineering was performed. Every claim is
web-sourced; claims that need confirmation against a real installation are marked below.

Tested application generation at research time: source code of the `main` branch at v0.86.0
(latest release, published 2025-08-09; E12). No newer release existed at access date. The two
history-file formats have been stable across the examined 0.x line (E13).

V1 requirement (spec 4.7): prefer `.aider.input.history` as the primary user-authored source;
`.aider.chat.history.md` is fallback only.

## 1. Platform status summary

Aider is a cross-platform Python CLI (pip/uv install). Its history persistence code has no
operating-system branch: both history files are created relative to the launch directory or git
root by the same `os.path.join` call on every OS (E2, E3, strong). Platform rows below therefore
share the same layout, but each platform still requires its own fixture/smoke verification
before its release gate passes; nothing here is inferred from another platform's *runtime*
behavior, only from the shared code path.

| Platform | Status | History file location |
|---|---|---|
| macOS | Supported; code-verified, needs smoke test | `<git-root>/.aider.input.history` and `<git-root>/.aider.chat.history.md`; launch cwd when not in a git repo |
| Linux (native) | Supported; code-verified, needs smoke test | Same rule |
| Windows (native) | Supported; code-verified, needs smoke test (CRLF check, section 4.2) | Same rule, Windows separators |
| WSL | Supported for files on the WSL filesystem; Windows-host project dirs under `/mnt/<drive>` are a separate opt-in scan root | Same rule |

There is no central per-user history store. History files live inside each project the user ran
Aider in. This drives the discovery model in section 2.

Aider maintenance status: last release 2025-08-09 (E12). A stale upstream reduces schema-drift
risk but the adapter still feature-detects rather than trusting version numbers.

## 2. Storage locations and discovery model

### 2.1 Default file placement (E2, E3, strong)

At startup Aider resolves the git root by searching parent directories of the cwd. Defaults:

```python
default_input_history_file = (
    os.path.join(git_root, ".aider.input.history") if git_root else ".aider.input.history"
)
default_chat_history_file = (
    os.path.join(git_root, ".aider.chat.history.md") if git_root else ".aider.chat.history.md"
)
```

- Inside a git repo: both files sit at the repository working-tree root.
- Outside a git repo: both files are created in the launch cwd.
- Aider offers to append `.aider*` to the repo `.gitignore` (`check_gitignore`, E3), so the
  files are normally untracked but present in the working tree.

### 2.2 Relocation and configuration (E2, E11, strong)

- `--input-history-file` / env `AIDER_INPUT_HISTORY_FILE` relocate the input history.
- `--chat-history-file` / env `AIDER_CHAT_HISTORY_FILE` relocate the chat markdown.
- The same keys can be set in `.aider.conf.yml`, loaded from cwd, git root, then home (E3).
- There is no option that disables writing either history file; pointing the option at
  a path Aider cannot create merely disables that file for the session with a warning (E1, E2).
- `--restore-chat-history` (default false) only controls *reading* the chat markdown back at
  startup; it does not change what is written (E2, E6).
- `--llm-history-file` (default: not written) logs raw LLM traffic to a separate file; it is
  never read by this adapter (section 3).
- `--encoding` (default `utf-8`) applies to the chat markdown; the input history is always
  UTF-8 regardless (section 4).

Users sometimes configure one global history file shared across projects (feature discussion
upstream, E14, weak). Such a file is discovered only through the env vars or an explicit root.

### 2.3 Discovery model: filename-pattern scan plus environment overrides

Because instances are scattered across project directories, `discover()` works as follows:

1. Read `AIDER_INPUT_HISTORY_FILE` and `AIDER_CHAT_HISTORY_FILE` from the discovery process
   environment. Each existing file becomes (part of) an instance. Environment variables are
   read; no config file is ever opened (section 3 — `.aider.conf.yml` can hold API keys).
2. Scan the configured search roots for files named exactly `.aider.input.history` or
   `.aider.chat.history.md`. Default root: the user home directory, bounded depth (default 6),
   pruning: `.git`, `node_modules`, `.venv`, `venv`, `.tox`, `.cache`, `.local/share/Trash`,
   `Library` (macOS), `AppData` (Windows), any directory that is itself a mount point of a
   network share, and in WSL everything under `/mnt/`. The user can add explicit extra roots
   (including a `/mnt/<drive>/...` project directory from WSL); plain text files are safe to
   read through DrvFS.
3. Each directory containing at least one of the two files is one source instance. The
   directory path is hashed (`path_hash`); private paths and project names are never returned.
   Opaque labels are `Aider 1`, `Aider 2`, ...
4. Channel selection per instance (spec 4.7): if `.aider.input.history` exists and parses, it
   is the only extraction channel for that instance. `.aider.chat.history.md` becomes the
   channel only when the input history is absent, unreadable, or unsupported. The two channels
   are never merged for one instance (the chat markdown duplicates every sent prompt as
   `#### ` lines; merging would double-count).

The scan must stay inside the five-minute onboarding budget (spec 2.1): the filename match
needs no file opens beyond directory listings, and candidate parsing streams each found file
once.

## 3. Files that must never be opened

The adapter opens only `.aider.input.history` and `.aider.chat.history.md` (plus the two
env-var-named equivalents). Everything else Aider writes is denylisted:

- `.aider.conf.yml` in cwd, git root, or home — YAML config; may contain provider API keys
  (E11). Never opened, even though it could point to relocated history files (accepted recall
  loss, section 10).
- `.env` files — Aider loads provider keys from them (E11). Never opened.
- `~/.aider/` and everything under it, notably `~/.aider/oauth-keys.env` (OpenRouter OAuth API
  key, E10), `~/.aider/analytics.json` (permanent analytics UUID, E9), and `~/.aider/caches/`.
- `.aider.llm.history` (or any `--llm-history-file` target) — raw LLM request/response log. It
  contains system prompts, injected repo content, and assistant output interleaved with user
  text and adds no user-authored records the two history files lack (E2).
- `.aider.tags.cache.v*/` — SQLite repo-map cache directories.
- `.aider.model.settings.yml`, `.aider.model.metadata.json`, and any other `.aider.*` entry
  not on the two-file allowlist.

## 4. Record schema by generation

### 4.1 `.aider.input.history` — primary channel

Format is defined by `prompt_toolkit.history.FileHistory`, which Aider uses directly without
subclassing (E1, E4, strong). Aider passes `FileHistory(input_history_file)` to its
`PromptSession`, so every accepted prompt-line is appended automatically; a separate
`add_to_input_history()` helper appends through the same class (E1).

On-disk format, from `FileHistory.store_string` (E4, strong):

```text
\n# <str(datetime.datetime.now())>\n
+<line 1 of entry>\n
+<line 2 of entry>\n
...
```

- One appended entry = one timestamp comment line followed by one `+`-prefixed line per line of
  the entry. A multi-line prompt (multiline mode, Alt-Enter) is a single entry with several
  `+` lines.
- Timestamp: `str(datetime.now())`, e.g. `2026-01-15 10:30:00.123456`. Local time, naive, no
  timezone. `str()` omits the fractional part when the microsecond field is exactly zero, so
  the parser accepts `YYYY-MM-DD HH:MM:SS(.ffffff)?`. The timestamp is written at append time,
  i.e. when the user submitted the entry.
- Encoding: hard-coded UTF-8 with `errors="replace"` on both write and read, in binary append
  mode — independent of `--encoding`, and no CRLF translation on any OS (E4).
- Read-back semantics (mirrored by the adapter): any line starting with `+` continues the
  current entry; any other line (timestamp comment, blank) terminates it. Entry text = joined
  `+` lines minus the trailing newline. `prompt_toolkit` itself ignores the `#` lines; the
  adapter associates each entry with the nearest preceding parsable `# ` timestamp line.
- No rotation, truncation, size cap, or cleanup exists anywhere in Aider or `prompt_toolkit`;
  the file grows for the life of the project unless the user deletes it (E1, E2, E4; absence
  of code is the evidence).
- Generation stability: this format is owned by `prompt_toolkit` 3.x (Aider's dependency) and
  is identical across the Aider 0.x line examined; no migration or alternate generation is
  known (E13, moderate for the earliest 2023 versions, strong for 0.35+).

What enters the input history (E1, E5, E6, strong):

- Every line accepted at the interactive prompt: natural-language prompts, `/commands`
  (including `/add`, `/ask ...`, `/voice`), and `!shell` passthrough lines.
- Voice: `/voice` transcribes with `whisper-1` via litellm and returns the raw transcript
  unmodified; since v0.69.0 the transcript is placed in the prompt buffer as editable
  placeholder text, so an accepted transcript is stored like any typed entry, structurally
  indistinguishable from typed text (E5, E8, E13).
- LLM-suggested shell commands the user approves are appended by Aider itself as
  `/run <command>` entries — machine-generated text in the input history (E6). Excluded by the
  `/` rule (section 6).
- NOT in the input history: clipboard bodies from `/paste` (only the `/paste` line itself,
  E5), `--message`/`--message-file` scripted prompts, and `/load`-replayed commands (E6 shows
  the only non-prompt append is the `/run` case). These reach only the chat markdown.

### 4.2 `.aider.chat.history.md` — fallback channel

Markdown transcript appended by `InputOutput.append_chat_history` (E1, strong):

- Session banner: `# aider chat started at YYYY-MM-DD HH:MM:SS` written at every launch
  (local naive time).
- User input: `user_input()` writes each sent message prefixed `#### `; internal newlines are
  re-joined as `  \n#### `, so one multi-line message becomes consecutive `#### ` lines. An
  empty message is written as `#### <blank>`.
- Assistant output: written raw (no prefix) by `ai_output()`.
- Tool/system messages (announcements, warnings, command echo): blockquoted `> ` lines by
  `tool_output()`.
- Encoding: the `--encoding` value (default UTF-8) with `errors="ignore"`, text append mode —
  so native-Windows files may contain CRLF line endings; the parser must accept `\r\n`.
- No rotation or truncation; `--max-chat-history-tokens` only bounds the in-memory summary
  sent to the model, not the file.
- Aider's own fallback parser (`utils.split_chat_history_markdown`, used by
  `--restore-chat-history`) applies the same line rules: `#### ` → user, `> ` → tool, `# ` →
  skipped, everything else → assistant (E6, E7). This corroborates the attribution rules but
  also shows the format's known ambiguity: an assistant answer containing a line that starts
  with `#### ` (a Markdown h4, inside or outside a code fence) is misattributed as user text
  by the naive rule. The adapter therefore adds fence tracking (section 6.2) and marks this
  channel with reduced authorship confidence.

### 4.3 Other files

`.aider.llm.history` and all cache/config files are out of scope (section 3). No SQLite, WAL,
compression, or encryption exists in either history file; spec 4.6 database rules do not apply.

## 5. Provenance of text fields

- Input-history entries: original, verbatim, as-submitted user input. Aider performs no
  spell-check, rewrite, or enhancement of prompt text before persistence (E1, E4). Text
  status: `verbatim`.
- Voice transcripts: raw `whisper-1` ASR output, returned without cleanup (E8), optionally
  edited by the user before submission, then stored identically to typed text. Because
  accepted entries carry no origin marker, positive voice matching is impossible; per spec 5.5
  every extracted Aider utterance has modality `written` (input-provenance convention).
- `/run <command>` entries appended by Aider after LLM suggestions: machine-generated (E6).
  Excluded structurally by the `/` rule.
- Chat-markdown `#### ` lines: duplicate of the sent user message, written verbatim at send
  time, but attribution is prefix-based and can misfire on assistant Markdown (4.2). Text
  status `verbatim`, authorship confidence lower than the primary channel.
- Chat-markdown non-`####` text: assistant-generated or tool output. Never extracted.
- Injected context (repo map, file contents, system prompts) never enters either history file;
  it exists only in the LLM messages and the optional `.aider.llm.history` (E1, E2).

## 6. Inclusion and exclusion rules

### 6.1 Primary channel (`.aider.input.history`)

Keep an entry as candidate user-authored text only if all hold:

1. The entry parsed from one or more `+` lines (section 4.1).
2. Trimmed text is non-empty.
3. Trimmed text does not start with `/` (drops all commands, including Aider-appended
   `/run ...`, `/voice`, and prose-bearing commands such as `/ask how do I ...` — a V1
   precision-over-recall decision, see section 10).
4. Trimmed text does not start with `!` (shell passthrough) or `{` when it matches the legacy
   brace multiline-block opener exactly (`{` alone on the first line).

Extraction metadata per kept entry: timestamp from the associated `# ` line (naive local time,
flagged `timezone_unknown`; entries with no preceding parsable timestamp line are `undated`),
modality `written`, text status `verbatim`, authorship basis `input_history_prompt_entry`,
authorship confidence high (structural: only prompt submissions and the excluded `/run`
appends ever reach this file).

Paste caveat: text pasted directly into the terminal prompt (not `/paste`) is typed-buffer
content and is stored like typed text. The adapter flags entries above a length threshold with
`content_flags: ["possible_paste"]`; the shared normalization layer removes copied/code-like
material (spec 4.5).

### 6.2 Fallback channel (`.aider.chat.history.md`)

Used only when the instance has no usable input history. Line-classification state machine:

1. Track fenced code blocks: a line whose first non-space characters are ``` or ~~~ toggles
   fence state. Inside a fence, every line is non-user content regardless of prefix.
2. Outside fences: `# aider chat started at <ts>` starts a new pseudo-session and provides its
   timestamp; other `# ` lines are skipped; consecutive `#### ` lines form one user message
   (strip the prefix, join with `\n`); `> ` lines and all other lines are tool/assistant
   content and are never extracted.
3. Drop user messages that are `<blank>`, empty after trimming, or start with `/` or `!`.

Metadata: timestamp = session banner time (per-message timestamps do not exist in this file),
flagged `session_start_time_only`; modality `written`; text status `verbatim`; authorship
basis `chat_markdown_user_prefix`; authorship confidence moderate (prefix-based attribution,
4.2 ambiguity); `content_flags: ["fallback_channel"]`.

### 6.3 Credential hygiene

The adapter never opens the section 3 denylist. Secrets typed by the user into prompts are
handled by the downstream normalization and privacy stages, not by the adapter.

## 7. Sessions, timestamps, and deduplication

- Sessions: the input history has no session markers; the adapter emits one pseudo-session per
  file (`session_hash` = salted hash of the canonical file path). Chat-markdown fallback
  sessions come from `# aider chat started` banners (banner index appended to the hash input).
- Timestamps: naive local time in both files. The adapter stores them as timezone-unknown
  local timestamps; period filtering compares them against local-time period boundaries and
  documents the imprecision. Entries without timestamps are counted but reported `undated`.
- Within-instance duplication: none in the primary channel (append-only, one entry per
  submission). The chat markdown duplicates primary-channel text, which is why the channels
  are never merged (2.3). Re-running `/load` scripts or `--message` loops can produce repeated
  identical chat-markdown lines; exact-hash dedup inside the normalizer collapses them only
  when timestamps also collide (same banner) — genuinely repeated language across sessions is
  preserved (spec 4.8).
- Cross-instance duplication: the same physical file reachable through two scan roots or a
  symlink is collapsed by canonical-path resolution before hashing. A copied project directory
  (backup) yields identical entry text and timestamps; the normalizer's exact-hash plus
  timestamp rule collapses these to the earliest-discovered canonical copy.
- Cross-source duplication (for example dictating with Wispr Flow and pasting into Aider) is
  the shared normalizer's job (spec 4.8); this adapter only supplies text hashes and
  timestamps.

## 8. Safe discovery, snapshot, extraction, verification

### 8.1 discover()

Read-only, local, no network, no model. Enumerate instances per 2.3. For each instance,
stream-parse the selected channel file once, applying section 6 rules to accumulate candidate
message, word, and byte counts plus min/max timestamps. Tolerate malformed content per
section 9. Return only `InstanceInventorySummary` (opaque label, counts, date range, channel
fingerprint); never a path, project name, or text. Storage-variant fingerprint:
`input-history-v1`, `chat-markdown-v1`, or `chat-markdown-v1-fallback` plus the observed
line-ending style and whether any timestamp line failed to parse.

### 8.2 snapshot()

Both files are plain append-only text; no locking exists. Snapshot = byte copy of the selected
channel file into `<repository>/runtime/runs/<run-id>/snapshots/aider/<instance-hash>/` after
the spec 3.6 preflight (containment, symlink refusal, git-ignore check, cloud-sync refusal).
A live Aider session may append mid-copy: the snapshot reader drops an incomplete final entry
(input history: trailing `+` block not terminated by a newline; chat markdown: unterminated
final line) with a `truncated_tail` note. Snapshot manifest records size, mtime, and SHA-256
per file. Copies are written mode 0600 in 0700 directories. Never write, lock, rename, or
delete anything in the source project directory.

### 8.3 extract()

Runs only against the snapshot. Emits `NormalizedUtterance` records per spec 4.4 with the
section 6 metadata and a `source_path_hash` of the canonical original path.

### 8.4 verify()

Adapter-specific deterministic checks: every utterance maps back to a snapshot byte range that
parses to the same text; no utterance starts with `/` or `!`; no utterance originates from a
fenced region (fallback channel); opened-path audit shows only allowlisted files; counts are
internally consistent; every timestamp is within the file's observed range.

## 9. Failure behavior

- Input history that decodes but contains no `+` line and is non-empty: `detected, unsupported
  schema` for that file; the instance falls back to the chat markdown if present, else reports
  unsupported.
- Unparsable timestamp lines: the affected entries are `undated`; the file stays supported.
  If more than 50% of entries are undated, add a `timestamp_parse_degraded` diagnostic.
- Chat markdown with neither a `# aider chat started` banner nor any `#### ` line: zero
  candidates; reported `found, empty` (not an error). Non-empty file with prefix lines that
  never form a valid structure (for example `#### ` lines only inside unbalanced fences):
  `detected, unsupported schema`.
- Undecodable bytes: input history uses `errors="replace"` like the writer; if replacement
  characters exceed 1% of characters, flag `encoding_degraded` and quarantine the file
  (`unsupported_schema`) rather than extract mojibake. Chat markdown is attempted as UTF-8
  with the same threshold (a non-UTF-8 `--encoding` configuration cannot be detected without
  opening config files; fail closed).
- Permission errors on a discovered file: instance state `inaccessible` with diagnostic code;
  never retried with elevated privileges.
- Environment-variable path that does not exist: ignored silently (no instance, no error).
- The adapter never guesses: any shape not matching section 4 yields exclusion plus a countable
  diagnostic.

## 10. Unresolved questions and required behavior when evidence is insufficient

1. Pre-v0.69.0 `/voice` behavior: whether transcripts submitted directly (before the
   placeholder mechanism, E13) were also appended to the input history is unverified for
   0.12–0.68 files. Required behavior: none — modality is `written` by spec 5.5 either way;
   entries are extracted identically.
2. Prose-bearing commands (`/ask ...`, `/architect ...`, `/code ...`) contain user-authored
   English after the command word. V1 excludes all `/` entries (6.1). A future revision may
   allowlist these three prefixes; until then this is a documented recall loss, not a guess.
3. Earliest-generation files (2023, Aider < 0.35, repo then `paul-gauthier/aider`): the
   filenames and formats appear unchanged, but no release-note or code audit of every early
   tag was performed (E13, moderate). Required behavior: the format parser is generation-free;
   any file that does not match section 4 becomes `detected, unsupported schema`.
4. A global history file configured only in `.aider.conf.yml` (not via env var) is invisible
   because config files are never opened (section 3). Required behavior: accepted recall loss;
   the user can add the file's directory as an explicit scan root.
5. Non-UTF-8 `--encoding` chat markdown cannot be identified without reading config. Required
   behavior: the 1% replacement-character threshold in section 9 fails the file closed.
6. Windows CRLF in chat markdown is inferred from text-mode `open("a")` semantics, not yet
   observed on a native Windows installation. Required behavior: the parser accepts both `\n`
   and `\r\n` unconditionally; the Windows release gate includes a real-file check.
7. Third-party forks (e.g. tools embedding Aider) may write compatible files with different
   names or extra content. Required behavior: only the two exact filenames (plus env-var
   overrides) are ever treated as Aider instances.

## 11. Test matrix and synthetic fixture plan

Fixtures live under `fixtures/aider/<variant>/` with `fixture.json` metadata. All content is
synthetic; secret-looking values are unmistakably fake.

| Variant | Contents | Expected result |
|---|---|---|
| `success-input-history` | Entries with microsecond and second-precision timestamps, a multi-line entry, `/add`, `/ask with prose`, `/run` (machine-appended), `!ls`, an empty `+` line inside an entry, an entry with no preceding timestamp | Only plain prompts kept; commands and shell lines excluded; undated entry flagged; exact counts and text hashes golden-asserted |
| `success-chat-md-fallback` | No input history; chat markdown with two `# aider chat started` banners, single- and multi-line `#### ` messages, `#### <blank>`, `> ` tool lines, assistant prose, an assistant code fence containing a `#### fake` line, an assistant h4 heading outside a fence | Fence line never extracted; heading line misattribution documented by golden file; sessions split per banner; fallback flags set |
| `channel-priority` | Both files present with overlapping content | Only the input history is parsed; chat markdown untouched in the opened-path audit |
| `empty` | (a) zero-byte input history; (b) chat markdown with banner only; (c) directory matched by env var but file missing | Zero candidates, `found, empty`; env-var miss ignored |
| `malformed` | (a) input history with binary garbage; (b) truncated final `+` block without newline; (c) chat markdown with unbalanced fences around all `#### ` lines | (a) unsupported after replacement threshold; (b) tail dropped with `truncated_tail`; (c) unsupported schema |
| `encoding` | Input history with UTF-8 multibyte text plus one invalid byte; chat markdown in CRLF form | Replacement below threshold keeps file; CRLF parsed identically |
| `env-override` | `AIDER_INPUT_HISTORY_FILE` pointing at a relocated file outside any scan root | Discovered as its own instance |
| `scan-pruning` | Tree containing decoy `.aider.input.history` under `node_modules/` and `.git/` plus one real project | Pruned dirs skipped; one instance found |
| `denylist` | Project dir with fake `.aider.conf.yml` (`openai-api-key: sk-FAKEFAKEFAKE0000`), `.env`, `.aider.llm.history`, `~/.aider/oauth-keys.env` layout | Opened-path audit proves none were read |
| `dedup-copy` | Two directories, one a byte-copy of the other | Canonical-path plus hash/timestamp dedup keeps one canonical set |

Platform test axes: all variants parse identically on macOS, Linux, native Windows, and WSL
runners; path handling covers Windows separators, home-relative roots, and the WSL `/mnt`
opt-in rule. A private smoke test against at least one real installation per claimed platform
is required before stable release; its outputs are never committed.

## 12. Reproducible read-only inspection commands

Safe on a real installation (structure only; never prints entry text). Placeholder paths only.

```bash
PROJ=<path-to-a-project-that-used-aider>

# Which Aider files exist (names only)
ls -la "$PROJ" | grep -E '^\S+ .*\.aider' | awk '{print $NF}'

# Input history: line-class census (no content)
python3 -c 'import sys,re,collections
c=collections.Counter()
for line in open(sys.argv[1],"rb"):
    s=line.decode("utf-8","replace")
    if s.startswith("+"): c["entry_line"]+=1
    elif re.match(r"^# \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?\s*$", s): c["timestamp"]+=1
    elif not s.strip(): c["blank"]+=1
    else: c["other"]+=1
print(dict(c))' "$PROJ/.aider.input.history"

# Input history: entry count and count of command entries (prefix class only)
python3 -c 'import sys
n=cmd=0; prev=False
for line in open(sys.argv[1],"rb"):
    s=line.decode("utf-8","replace")
    if s.startswith("+"):
        if not prev:
            n+=1
            if s[1:].lstrip().startswith(("/","!")): cmd+=1
        prev=True
    else: prev=False
print("entries",n,"command-or-shell",cmd)' "$PROJ/.aider.input.history"

# Chat markdown: line-class census (no content)
python3 -c 'import sys,collections
c=collections.Counter(); fence=False
for s in open(sys.argv[1],encoding="utf-8",errors="replace"):
    t=s.lstrip()
    if t.startswith("```") or t.startswith("~~~"): fence=not fence; c["fence"]+=1
    elif fence: c["in_fence"]+=1
    elif s.startswith("# aider chat started"): c["banner"]+=1
    elif s.startswith("#### "): c["user_line"]+=1
    elif s.startswith("> "): c["tool_line"]+=1
    elif s.startswith("# "): c["header"]+=1
    else: c["assistant_or_blank"]+=1
print(dict(c))' "$PROJ/.aider.chat.history.md"
```

Never run `cat`, `grep` with content output, or any command that prints entry values against
real history files.
