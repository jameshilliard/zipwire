"""Backend readers with lazy imports.

No backend library is imported until its reader class is accessed.

Usage::

    from zipwire.backends import Httpx2SyncReader
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zipwire.backends._aiobotocore import AiobotocoreReader as AiobotocoreReader
    from zipwire.backends._aiohttp import AiohttpReader as AiohttpReader
    from zipwire.backends._file import AsyncFileReader as AsyncFileReader
    from zipwire.backends._file import FileReader as FileReader
    from zipwire.backends._httpx2 import Httpx2AsyncReader as Httpx2AsyncReader
    from zipwire.backends._httpx2 import Httpx2SyncReader as Httpx2SyncReader
    from zipwire.backends._requests import RequestsReader as RequestsReader
    from zipwire.backends._urllib3 import Urllib3Reader as Urllib3Reader

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AiobotocoreReader": ("zipwire.backends._aiobotocore", "AiobotocoreReader"),
    "Httpx2SyncReader": ("zipwire.backends._httpx2", "Httpx2SyncReader"),
    "Httpx2AsyncReader": ("zipwire.backends._httpx2", "Httpx2AsyncReader"),
    "AiohttpReader": ("zipwire.backends._aiohttp", "AiohttpReader"),
    "Urllib3Reader": ("zipwire.backends._urllib3", "Urllib3Reader"),
    "RequestsReader": ("zipwire.backends._requests", "RequestsReader"),
    "FileReader": ("zipwire.backends._file", "FileReader"),
    "AsyncFileReader": ("zipwire.backends._file", "AsyncFileReader"),
}

__all__ = list(_LAZY_IMPORTS)


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return __all__
