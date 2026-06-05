# Security Policy

## Supported versions

`quest-ai-runner` is pre-1.0. Security fixes land on `main` and in the latest released version.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through either channel:

- **GitHub Security Advisories** — use the *"Report a vulnerability"* button on the
  [Security tab](https://github.com/SpiritualData/quest-ai-runner/security/advisories/new)
  of this repository. This is the preferred channel.
- **Email** — `security@spiritualdata.org`, with `quest-ai-runner` in the subject line.

Please include: affected version/commit, a description of the issue, reproduction steps or a
proof of concept, and the impact you anticipate. We aim to acknowledge reports within 5 business
days and to provide a remediation timeline after triage.

## Scope notes for this project

`quest-ai-runner` executes AI tasks and can spawn deep, goal-driven runs. A few things are the
**operator's** responsibility, not library bugs:

- **Credentials.** The library reads the Quest key (`qsk_...`) and `ANTHROPIC_API_KEY` from the
  environment. Never hardcode or commit them. Treat the executor key as the runner's identity.
- **The deep-runner is powerful.** `SubprocessGoalRunner` launches a coding agent (Claude Code) in
  a working directory you configure, by default with permission prompts skipped for headless use.
  Scope its `working_dir` and tool gating (`allowed_tools` / `disallowed_tools`) to what the lane
  actually needs.
- **Retrieval is read-only and root-scoped.** `FilesAdapter` hard-scopes reads inside the
  configured root and skips secret-ish/binary/oversize files. Keep secrets out of any corpus root
  you point it at anyway.

If you find a way these protections can be bypassed, that **is** in scope — please report it.
