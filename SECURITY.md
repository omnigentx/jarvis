# Security Policy

## Supported Versions

The latest release on `main` is the only actively supported version. Older tags do not receive security backports.

## Reporting a Vulnerability

**Do not file a public GitHub issue for security bugs.**

Instead, please use [GitHub Security Advisories](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) — open the **Security** tab of this repository and click **"Report a vulnerability"**.

When reporting, include:

- A description of the vulnerability and its impact
- Steps to reproduce (or a proof-of-concept)
- The affected version / commit SHA
- Any suggested mitigation

You should expect an initial acknowledgement within **5 business days**. If you don't hear back within that window, please follow up via the same advisory thread.

## Disclosure

We aim to release a fix and public advisory within **30 days** of triage. Coordinated disclosure with the reporter is preferred — credit will be given in the advisory unless you prefer to remain anonymous.

## Out of scope

- Issues in upstream submodules (`fast-agent`, `figma-ui-mcp`, `mcp-atlassian`) should be reported to those projects directly.
- Self-hosting misconfiguration (e.g. exposing the backend on a public IP without authentication) — these are user responsibilities documented in [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md).
- API keys you commit to your own fork. Rotate them at the provider; the repo cannot help recover them.
