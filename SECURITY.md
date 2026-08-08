# Security Policy

## Reporting a vulnerability

Report security vulnerabilities privately to **security@glite.ai**.

Do not open public GitHub issues, pull requests, or discussions for vulnerabilities. Public
reports can expose users before a fix exists.

Include what you can: affected component, reproduction steps, impact, and environment
(OS, agent runtime, versions). Synthetic examples only — never attach real transcripts or
personal data to a report.

## Scope

Glite English Audit is local-first, not fully local. It reads local application data, does all
deterministic work on the user's machine, sends selected text for analysis through the user's own
Codex or Claude Code session to that user's AI provider, and submits only an explicitly reviewed,
allowlisted package to Glite. Reports are especially welcome for:

- Data-exposure paths: any way source text, transcripts, private paths, or credentials could
  reach logs, artifacts, committed files, or agent-visible output where they should not.
- Loopback review server issues: access from other origins or hosts, request forgery, or
  serving private data beyond the local review page.
- Submission-boundary leaks: any way content outside the submission allowlist could enter a
  submission package or leave the machine.
- Prompt-injection paths that cause the audit to act on instructions embedded in source text.

## Review server threat model

The final review page is the only browser surface in the project
(`src/glite_english_audit/review_server/`). It defends against other origins and other hosts, not
against a hostile local account:

- The socket binds only to `127.0.0.1`. No other interface is offered.
- Every route sits under a per-run random token path. A wrong token returns the same 404 body as any
  unknown path, and requests are never logged, because the URL carries the token.
- Every POST also requires that token in an `X-Glite-Review` header, which a cross-site form or
  fetch cannot supply, and a present `Origin` header must name this server exactly.
- Only the review routes exist; no other private artifact is reachable. Responses carry a strict
  Content-Security-Policy and `Cache-Control: no-store`.
- The server shuts down after 30 minutes without a request.

Out of scope by design: another user or process on the same machine with the user's privileges. V1
relies on operating-system account and disk security and adds no application-level encryption to
private run artifacts. Report anything that lets a different origin, a different host, or an
unrelated local path reach review data.

## Response

We will acknowledge your report, investigate, and keep you informed of progress until the issue
is resolved. Please allow reasonable time for a fix before any public disclosure, and coordinate
disclosure timing with us.
