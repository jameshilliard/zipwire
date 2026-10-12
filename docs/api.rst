API Reference
=============

Core
----

.. note::

   ``SyncRemoteZip`` and ``AsyncRemoteZip`` **must** be used as context
   managers.  The archive is loaded on entry and all internal state is
   cleared on exit.  Calling methods like :meth:`~zipwire.SyncRemoteZip.read`
   outside a ``with`` / ``async with`` block raises :exc:`RuntimeError`.

.. warning::

   ``SyncRemoteZip`` and ``AsyncRemoteZip`` instances are **not thread-safe**.
   Each thread or task should use its own instance.

All archive constructors accept ``metadata_encoding=None``. The default is
CP437 for legacy names; the UTF-8 flag and valid Unicode Path extra fields
retain their specified meanings. ``read(name, pwd=None)`` and
``read_into(name, dest, pwd=None)`` accept traditional ZIP passwords as
bytes. ``read_into`` takes ``pwd`` as a keyword argument. ``setpassword``
sets the default password; ``None`` or an empty password clears it.
``comment`` returns the archive comment.

End-record selection follows stdlib ``zipfile``: a final record without
a comment takes precedence; otherwise the parser uses the last signature
in the ZIP comment search window. This also supports trailing data, but
signatures embedded in comments can prevent the archive from being read.
An incomplete archive comment returns the available bytes. ZIP64 values
replace placeholders in the legacy end record, as described in the
`ZIP specification <https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT>`_.

The async readers use asyncio scheduling checkpoints during decompression
and decryption. This also makes buffered and cached reads cancellable
between decoding steps. Each codec call and destination ``write`` still
executes synchronously; these checkpoints are not a hard latency guarantee.

.. autoclass:: zipwire.SyncRemoteZip
   :members:
   :inherited-members:
   :special-members: __enter__, __exit__

.. autoclass:: zipwire.AsyncRemoteZip
   :members:
   :inherited-members:
   :special-members: __aenter__, __aexit__

Wheel
-----

.. autoclass:: zipwire.SyncRemoteWheel
   :members:
   :inherited-members:
   :show-inheritance:

.. autoclass:: zipwire.AsyncRemoteWheel
   :members:
   :inherited-members:
   :show-inheritance:

.. autoclass:: zipwire.RemoteZipInfo
   :show-inheritance:

.. autoclass:: zipwire.EOCDInfo
   :members:

``cd_offset`` is an absolute offset in the containing file.
``archive_offset`` adjusts stored offsets to positions in the containing
file, normally accounting for prepended data; ``comment`` is the archive
comment as bytes. ZIP64 metadata outside the initially fetched tail is
loaded with additional bounded range requests.

Protocols and Types
-------------------

.. autoclass:: zipwire.SyncReader
   :members:

.. autoclass:: zipwire.AsyncReader
   :members:

.. autoclass:: zipwire.Headers
   :members:

.. autoclass:: zipwire.Writable
   :members:

``read_into`` retries partial writes. A destination's ``write`` method
returns the number of bytes accepted, or ``None`` when it has consumed
the entire chunk. Zero, negative, oversized, or non-integer counts raise
``OSError``. A nonblocking destination must raise ``BlockingIOError``
instead of returning ``None`` when it would block. Async readers yield
between write attempts. The caller remains responsible for flushing and
closing the destination.

.. autoclass:: zipwire.CompressionMethod
   :members:

Backends
--------

.. autoclass:: zipwire.backends._urllib3.Urllib3Reader
   :members:

.. autoclass:: zipwire.backends._httpx2.Httpx2SyncReader
   :members:

.. autoclass:: zipwire.backends._httpx2.Httpx2AsyncReader
   :members:

.. autoclass:: zipwire.backends._requests.RequestsReader
   :members:

.. autoclass:: zipwire.backends._aiohttp.AiohttpReader
   :members:

.. autoclass:: zipwire.backends._file.FileReader
   :members:
   :special-members: __enter__, __exit__

.. autoclass:: zipwire.backends._file.AsyncFileReader
   :members:
   :special-members: __aenter__, __aexit__

Exceptions
----------

.. code-block:: text

   Exception
   └── ZipwireError
       ├── BadZipFile (also inherits zipfile.BadZipFile)
       │   └── CRCMismatch
       ├── UnsupportedCompression (also inherits NotImplementedError)
       ├── FileTooLarge
       ├── RangeRequestUnsupported
       └── FileNotFoundInZip (also inherits KeyError)

Missing or incorrect passwords raise ``RuntimeError``; passwords of an
unsupported type raise ``TypeError``. Unsupported extraction versions,
strong encryption, and patched-data flags raise ``NotImplementedError``.
An unavailable optional compression module raises ``RuntimeError`` when
reading an entry that needs it; other codecs remain usable.
Invalid LZMA properties raise ``lzma.LZMAError``, as in stdlib ``zipfile``.
Property validation waits for the complete header and a payload byte,
preserving stdlib's tolerance of incomplete headers for empty members.

Invalid ZIP headers and CRC mismatches raise ``BadZipFile``. As in current
stdlib ``zipfile``, duplicate references to a single local header warn
with ``UserWarning``; other overlapping entries raise ``BadZipFile``.
Directory size determines the entry list rather than the advisory entry
count. Incomplete comments and compressed streams can be read when
``zipfile`` tolerates them: output is limited to the declared uncompressed
size, and its CRC must match even when fewer bytes are returned.
Compressed members continue decoding to the codec's end marker or the
declared compressed extent, discarding excess output in bounded chunks.
An understated output size therefore cannot hide codec errors in the
remaining compressed data. Async reads keep yielding during this
validation even when no more output is written.
If the source ends before the declared compressed size has been received
and neither the codec's end marker nor the declared output size has been
reached, reading raises ``EOFError``. This preserves zipfile's tolerance
of short responses that already produced the declared output. Stored
entries can stop reading at the output limit. Archives shorter than
the minimum 22-byte end record raise ``BadZipFile`` before any range request.

.. autoexception:: zipwire.ZipwireError
   :show-inheritance:

.. autoexception:: zipwire.BadZipFile
   :show-inheritance:

.. autoexception:: zipwire.UnsupportedCompression
   :show-inheritance:

.. autoexception:: zipwire.CRCMismatch
   :show-inheritance:

.. autoexception:: zipwire.FileTooLarge
   :show-inheritance:

.. autoexception:: zipwire.RangeRequestUnsupported
   :show-inheritance:

.. autoexception:: zipwire.FileNotFoundInZip
   :show-inheritance:
