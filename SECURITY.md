# Security Policy

## Reporting a vulnerability

Report security vulnerabilities privately to **security@glite.ai**.

Do not open public GitHub issues, pull requests, or discussions for vulnerabilities. Public
reports can expose users before a fix exists.

Include what you can: affected component, reproduction steps, impact, and environment
(OS, agent runtime, versions). Synthetic examples only — never attach real transcripts or
personal data to a report.

## Scope

Glite English Audit is a local-first tool. It reads local application data, analyzes it on the
user's machine, and submits only an explicitly reviewed, allowlisted package. Reports are
especially welcome for:

- Data-exposure paths: any way source text, transcripts, private paths, or credentials could
  reach logs, artifacts, committed files, or agent-visible output where they should not.
- Loopback review server issues: access from other origins or hosts, request forgery, or
  serving private data beyond the local review page.
- Submission-boundary leaks: any way content outside the submission allowlist could enter a
  submission package or leave the machine.
- Prompt-injection paths that cause the audit to act on instructions embedded in source text.

## Response

We will acknowledge your report, investigate, and keep you informed of progress until the issue
is resolved. Please allow reasonable time for a fix before any public disclosure, and coordinate
disclosure timing with us.
