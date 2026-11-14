# Contributing to zipwire

Thanks for your interest in zipwire! Project setup, development commands, code
style, documentation requirements, changelog conventions, and commit sign-off
are documented in [AGENTS.md](AGENTS.md); read it before you start. This
document covers the contribution workflow that is specific to people.

## Scope

zipwire is primarily built for two kinds of remote archives:

- **Python wheels on package indexes and other remote hosts** - reading wheel
  contents and `.dist-info` metadata (METADATA, WHEEL, RECORD) from PyPI.org,
  Pulp, GitHub, and similar sites.
- **Generic ZIP archives on standard HTTP servers** - any host that supports
  HTTP range requests.

Both are accessed through the common Python HTTP libraries supported by the
bundled backends. Proposals that go beyond this scope - other archive formats,
non-HTTP transports, or site-specific APIs - may still be useful, but please
open an issue to discuss them before writing code.

Special cases may belong in a third-party package rather than in zipwire
itself. The public API is being stabilised and can be extended to some degree
to make third-party features possible. If your idea is inherently site- or
format-specific, a separate package built on the public API is probably the
better home; open an issue if zipwire does not expose enough for it.

## Issues: features and bugs

Every new feature and every bug fix needs a GitHub issue, and the pull request
should reference it. Open the issue before writing code so the approach can be
agreed on first.

- **New features** need a concrete use case: what you are trying to accomplish,
  which API or workflow falls short today, and how the proposed change would
  help. A feature request without a use case is hard to evaluate and may be
  declined.
- **Bug fixes** need a minimal reproducer: the smallest archive or URL setup
  that triggers the problem, the code that fails, expected versus actual
  behaviour, and the zipwire and Python versions. The reproducer should become
  a regression test in the pull request.

## Pull requests

- Keep changes focused and separate unrelated refactors from behaviour changes.
- Run the tests, lint, and documentation build listed under
  [Commands](AGENTS.md#commands) before requesting review.
- Add a `CHANGELOG.md` entry for user-visible changes, following the
  [changelog rules](AGENTS.md#changelog-rules).
- Sign off commits with `git commit -s`, as required by the
  [git rules](AGENTS.md#git-rules).

## Reporting security issues

Please do not open a public issue for security problems. Use
[GitHub's private vulnerability reporting](https://github.com/tiran/zipwire/security/advisories/new)
as described in [SECURITY.md](SECURITY.md).
