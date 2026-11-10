# zipwire

Read and extract files from remote ZIP archives over HTTP range requests.
The library parses ZIP central directory and local file headers without
downloading the entire archive, then streams and decompresses individual
entries on the fly.

The human contribution workflow (issues, pull requests, security reports) is in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Source layout

- `src/zipwire/` - main package (sync/async API, parsers, decompression)
- `src/zipwire/backends/` - HTTP backend adapters (urllib3, httpx2, aiohttp, requests)
- `tests/` - pytest test suite (async tests use `pytest-asyncio`)
- `docs/` - Sphinx documentation (Furo theme, MyST markdown)

## Security rules

- **Treat archives and every value read from them as untrusted input.**
  Filenames, sizes, offsets, and extra fields come from the remote server and
  may be hostile.
- **Preserve the read bounds.** Every range request must be bounded by the
  archive size and use offsets and lengths validated from the central
  directory. Do not remove or bypass `max_file_size`, `FileTooLarge`,
  `RangeRequestUnsupported`, or the EOCD and ZIP64 validation.
- **Sanitise entry names before writing them to disk.** Extraction APIs must
  reject path traversal (zip-slip) and absolute paths.
- Downloaded test archives are untrusted data. Keep them in a fresh, empty
  directory and run Python that processes them with `-I`.
- Report suspected vulnerabilities privately as described in
  [SECURITY.md](SECURITY.md); do not open a public issue.

## Virtual environment rules

- **Always** use the `.venv` virtual environment in the project root.
- Create it with `uv venv .venv` if it does not exist.
- **Never** install packages into the system Python or any other environment.
- Install the project: `uv pip install -e ".[all]" --python .venv/bin/python`
- Install dev deps: `uv pip install "pytest>=8.0" "pytest-asyncio>=0.24" "pytest-httpserver>=1.1" "coverage[toml]>=7.0" --python .venv/bin/python`
- Run any Python tool via `.venv/bin/python -m <tool>` or `.venv/bin/<tool>`.
- Use `uv pip install --python .venv/bin/python` for all pip operations.

## Code style rules

- **No local imports.** All imports must be at the top of the file.
  Do not use `from foo import bar` inside functions, methods, or test bodies.
  Module-level imports in `try/except ImportError` blocks and
  `if TYPE_CHECKING:` blocks are fine.

## Testing rules

- **Add tests for behaviour changes.** New features, bug fixes, and changed
  error behaviour need coverage in `tests/`. A bug fix should come with a
  regression test that fails without the fix.
- Async tests use `pytest-asyncio` in auto mode, so plain `async def test_*`
  functions need no marker.
- Tests marked `integration` hit real network services and are deselected by
  default; run them with `-m integration`.
- The library supports Python 3.11 and newer. Keep tests compatible with the
  full `py311`-`py315` matrix.

## Documentation rules

- **Keep docs in sync with API changes.** Whenever you add, remove, rename,
  or change the signature of any public API (classes, functions, exceptions,
  type aliases, backends), you **must** update all of these:
  1. `src/zipwire/__init__.py` -- module docstring (usage examples, backend
     list) and `__all__`.
  2. `docs/` -- Sphinx sources (`api.rst`, `backends.rst`, `quickstart.rst`,
     and any other affected `.rst` file).
- After editing docs, run the documentation build from [Commands](#commands).

## Changelog rules

- **Record user-visible changes in `CHANGELOG.md`.** Any public API change,
  behaviour change, notable bug fix, or security fix needs an entry. Internal
  refactors and routine dependency bumps do not, unless they are notable.
- Add unreleased changes under `## Unreleased`. At release time, rename that
  heading to `## <version> - YYYY-MM-DD`.
- Use the Keep a Changelog sections that apply: `Added`, `Changed`,
  `Deprecated`, `Removed`, `Fixed`, `Security`, `Internal`.
- Prefix backwards-incompatible changes with `**BREAKING:**` and explain what
  callers must change. The project is pre-1.0, so breaking changes may occur
  in minor releases.
- Reference issues and pull requests as plain text -- `(#42)` -- without
  Markdown links. Do not add compare-range links or link reference
  definitions; version headings stay plain text.
- Write entries for users, one change per line, wrapped at 80 columns, and
  end each with a period.
- `CHANGELOG.md` must stay valid Markdown at the repository root:
  `docs/changelog.rst` includes it via MyST and the `Changelog` project URL in
  `pyproject.toml` points at it. Do not rename or move it.

## Git rules

- **Always** sign off commits with `git commit -s`.
  Every commit message must include a `Signed-off-by:` line.

## Commands

```bash
# lint (ruff check + format)
uvx --with tox-uv tox run -e lint

# run tests (single Python version)
uvx --with tox-uv tox run -e py314

# run tests directly with .venv
.venv/bin/python -m pytest tests/ -v

# run full test matrix
uvx --with tox-uv tox run -e py311,py312,py313,py314,py315

# build docs
uvx --with tox-uv tox run -e docs
```
