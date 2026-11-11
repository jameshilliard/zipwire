Scope and non-goals
===================

zipwire does one thing: it reads individual entries from ZIP archives that
live behind an HTTP server, using range requests instead of downloading the
whole file.

The scope is deliberately small.  zipwire would rather do one thing well than
grow into a general-purpose archive toolkit: keeping the feature set focused
makes the package easier to maintain and keeps feature creep out of the common
wheel-reading path.  This page describes what is in scope and what is
deliberately left out.

In scope
--------

The core target is a **standard ZIP archive on a standard HTTP server that
supports range requests**: the archive lives at a stable URL, the server
advertises ``Accept-Ranges: bytes``, and it honours ``Range: bytes=...``.
Without range support zipwire cannot read anything and raises
:exc:`~zipwire.RangeRequestUnsupported`.

Standard ZIP features are covered: central directory and ZIP64 parsing for
archives and entries beyond 4 GiB, reading an entry into memory, and
streaming an entry to a synchronous or asynchronous destination.  The
supported compression methods are ``STORED``, ``DEFLATED``, ``BZIP2``,
``LZMA``, and ``ZSTANDARD`` (method 93), subject to the availability of the
corresponding Python modules.

**Python wheels** are a first-class use case:
:class:`~zipwire.SyncRemoteWheel` and :class:`~zipwire.AsyncRemoteWheel`
fetch ``.dist-info`` entries from PyPI, Pulp, and other package index
implementations with a single adaptive tail request.  Because zipwire relies
only on standard HTTP range requests, any index that serves wheels from a
range-capable web server works without site-specific integration.

Local archives are supported through
:class:`~zipwire.backends.FileReader` and
:class:`~zipwire.backends.AsyncFileReader`.

Out of scope
------------

Other transports (S3, FTP, ...)
   zipwire reads over HTTP(S) range requests and plain file IO.  Other
   transports -- for example S3 through aiobotocore or botocore -- are out of
   scope.  The reader layer is pluggable, though: a third-party package can
   provide its own transport by implementing the :class:`~zipwire.SyncReader`
   or :class:`~zipwire.AsyncReader` protocol and passing the reader to
   :class:`~zipwire.SyncRemoteZip` or :class:`~zipwire.AsyncRemoteZip`.
   See :doc:`backends` for a custom reader example.
   Note that zipwire is pre-1.0 and its public API is not stable yet, so
   third-party readers may need updates between minor releases.

Encrypted archives
   zipwire has no password or decryption API.  Traditional ZipCrypto is
   cryptographically weak and should be considered broken.  WinZip AES is
   stronger, but neither is supported or planned.  Package indexes normally
   serve unencrypted wheels, and adding password handling would complicate an
   API built around reading a few ranges from a public URL.

``httpx`` package
   zipwire's HTTP/2 backend uses httpx2, the Pydantic community fork of the
   ``httpx`` package.  As of November 2026, the last stable upstream release
   of ``httpx`` was in December 2024, so zipwire provides no backend for it.

Rarely used and legacy ZIP features
   The parser targets archives produced by mainstream tools and package
   builders.  Legacy compression methods such as Shrunk, Implode, Deflate64,
   and PKWARE DCL Implode raise
   :exc:`~zipwire.UnsupportedCompression`.  Spanned or multi-disk archives,
   which split one logical archive across several files, are unsupported as
   well: a range request addresses a single file at a single URL.

Other archive formats and write support
   zipwire is a read-only ZIP client.  It does not create, modify, repair, or
   repack archives, and it does not read tar, 7z, RAR, or similar formats.
   Such features belong in a separate package or tool.

Legacy Zstandard method ID (20)
   ZIP APPNOTE 6.3.7 briefly assigned Zstandard to compression method 20.
   Version 6.3.8, revised fourteen days later, deprecated that ID and moved
   Zstandard to method 93, which is the only one zipwire supports.  Archives
   written during that short window -- or by tools that copied the draft --
   are rare; reading them raises :exc:`~zipwire.UnsupportedCompression`.

Have an idea?
-------------

If you are unsure whether a feature belongs in zipwire, `open an issue
<https://github.com/tiran/zipwire/issues>`_ and ask -- we are happy to talk
it through.  Some ideas that are out of scope here make excellent
third-party packages built on the reader protocols; see
`CONTRIBUTING.md <https://github.com/tiran/zipwire/blob/main/CONTRIBUTING.md>`_
for the contribution workflow.
