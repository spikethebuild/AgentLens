# Security policy

AgentLens runs inside developers' coding sessions and records what their agent
does, so privacy bugs are security bugs here.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability**
on this repository (private vulnerability reporting). Do not open a public
issue, and do not paste real secrets into any report; a made-up value that
shows the pattern is enough.

You should get a reply within 7 days. Fixes ship as a patch release, with
credit in the changelog if you want it.

## What counts

- A secret, file content, prompt text (with prompt capture off) or transcript
  message content reaching disk: the spool, the database, the log, the plan
  usage snapshot or any export.
- A redaction miss for a pattern AgentLens claims to cover (see "Privacy" in
  the README).
- Anything that corrupts, overwrites or drops a user's Claude Code settings,
  or edits entries that are not AgentLens's own.
- A hook, status line or pane path that can block Claude Code, hang it, or
  blank the status bar.
- Files AgentLens creates with permissions wider than 0700 (directories) or
  0600 (files).
- Any network traffic from AgentLens (it makes none by design).

## Supported versions

Only the latest release receives fixes while AgentLens is below 1.0.
