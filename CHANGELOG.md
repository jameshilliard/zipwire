# Changelog

All notable changes to zipwire are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
zipwire is pre-1.0, so backwards-incompatible changes can occur in minor
releases; they are marked **BREAKING**.

## Unreleased

### Added

- `AsyncRemoteZip.read_into()` accepts asynchronous writable objects
  implementing the new `AsyncWritable` protocol, such as `anyio.AsyncFile`.
  If `write()` returns an awaitable, zipwire awaits it before reading more
  input, so a slow destination applies backpressure to the transfer.
- New `UnsupportedFeature` exception. Entries that use encryption
  (general purpose flag bit 0), compressed patched data (bit 5), or
  strong encryption (bit 6) are now rejected up front instead of feeding
  unusable data to the decompressor and failing with a confusing CRC or
  size error.

### Security

- Header parsing and entry admission were hardened to match CPython's
  `zipfile`: offsets and sizes from the End of Central Directory record
  and the central directory are validated against the archive size
  before the central directory is fetched and before entry data is
  read, range fetches are checked for short responses, overlapped
  entries whose data runs into the next entry or the central directory
  (possible zip bomb) are rejected, end records that declare multiple
  disks are rejected (the ZIP64 EOCD record's own disk fields are
  ignored, as in CPython), and corrupt ZIP64 records, locators, and
  extra fields are rejected instead of being silently ignored or
  misparsed.
- The local file header's filename is now compared against the central
  directory name when an entry is read, as CPython's `zipfile` does in
  `ZipFile.open()`. An archive whose local header names a different
  member than the central directory, crafted so that scanners and
  extractors disagree about member names, is rejected with
  `BadZipFile`. The check covers the sync and async readers and the
  wheel tail fast path.

### Changed

- **BREAKING:** `SyncReader.stream_range()` and `AsyncReader.stream_range()`
  now return `Generator[bytes, None]` and `AsyncGenerator[bytes, None]`
  instead of `Iterator[bytes]` and `AsyncIterator[bytes]`. Custom backends
  must return generators so zipwire can close them when extraction stops
  early.
- **BREAKING:** `read_into()` now retries partial writes and validates
  what `write()` reports: a zero, negative, oversized, or non-integer
  byte count raises `OSError`. `Writable.write()` and
  `AsyncWritable.write()` must return the number of bytes accepted, or
  `None` to mean that the whole chunk was written. Destinations that
  return any other value, such as `True` as a success flag, now fail;
  they must be updated to return a byte count or `None`.

### Fixed

- Streamed HTTP responses and their pooled connections are now closed
  deterministically when a range request fails or extraction stops early,
  rather than waiting for generator finalization, and the reader is closed
  when the archive context manager fails to load.
- ZIP-LZMA (method 14) entries can be read again. The ZIP-LZMA payload
  header (version, property size, LZMA1 properties) is now parsed and the
  raw LZMA1 stream is decoded with a matching filter, instead of handing
  the payload to `lzma.decompress()`, which expects a container format
  and failed on every entry with `lzma.LZMAError` (#71). The dictionary
  size claim in the properties is clamped to 64 MiB, the largest
  dictionary real encoders such as xz ultra mode (preset 9) write.

### Security

- Streaming decompression now yields output in chunks of at most 2 MiB
  and verifies that compressed streams end cleanly and that the output
  matches the entry's declared size. Truncated or mismatched output is
  rejected with the new `TruncatedStream` and `SizeMismatch` exceptions,
  so a crafted archive can neither force a huge allocation nor make
  zipwire return damaged data. Credits to James Hilliard for the original
  implementation.

### Internal

- The library and test suite are now type checked with pyrefly and mypy,
  and type annotations were completed and improved throughout.
- `StreamingDecompressor` no longer writes to a destination; `feed()`
  returns decompressed bytes and `finish()` verifies that the stream ended
  cleanly and checks the CRC-32 and expected size without returning data,
  so the sync and async `read_into()` paths share these checks.

## 0.4.0 - 2026-08-29

### Added

- Local-file readers `FileReader` and `AsyncFileReader` (#40).

### Internal

- Hardened the GitHub Actions workflows with zizmor (#30).

## 0.3.0 - 2026-07-22

### Added

- `SyncRemoteWheel` and `AsyncRemoteWheel` for reading wheel `.dist-info`
  metadata (#29).

### Changed

- **BREAKING:** `AsyncRemoteZip.infolist()`, `namelist()`, and `getinfo()` are
  now regular synchronous methods. Archive metadata is loaded eagerly in
  `__aenter__` (#28).

## 0.2.0 - 2026-07-20

### Added

- `EOCDInfo` is now part of the public read-only API (#22).
- Public `url` property on readers and remote ZIP objects (#27).

### Changed

- **BREAKING:** `RemoteZip` instances must now be used as context managers;
  calling methods outside the context raises `RuntimeError` (#26).
- Use the standard library `compression.zstd` instead of the third-party
  `zstandard` package (#20).

### Internal

- Expanded the module docstring with usage examples and the backend list
  (#18).
- Added a minimal-dependency (urllib3-only) test matrix via tox (#19).
- Improved test coverage for `stream_range`, `FileTooLarge`, and error paths
  (#21).

## 0.1.0 - 2026-07-13

### Added

- HTTP/2 support in the httpx2 backend (#12).
- Debug logging in the CLI and backends (#13).
- `max_file_size` protection in the HTTP backends (#15).
- `--dirs` flag and improved CLI verbosity levels (#16).
- Documentation on Read the Docs (#7).

### Changed

- Reduced the number of HTTP requests for loading archives and reading entries
  (#8).
- Use absolute byte ranges instead of suffix ranges (#10).
- Enabled redirect-following for all backends (#14).

### Internal

- Added integration tests against PyPI (#11).
- Combined the httpx2 backend modules (#12).
- Increased test coverage from 68% to 98% (#9).
- Bumped the development status to beta, relaxed version pins, and added
  Python 3.15-dev testing (#17).

## 0.0.1 - 2026-07-11

### Added

- Initial release.
