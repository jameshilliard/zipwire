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

.. autoclass:: zipwire.AsyncWritable
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
       ├── BadZipFile
       ├── UnsupportedCompression
       ├── CRCMismatch
       ├── FileTooLarge
       ├── RangeRequestUnsupported
       └── FileNotFoundInZip (also inherits KeyError)

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
