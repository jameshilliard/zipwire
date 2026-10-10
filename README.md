# zipwire

Extract individual files from remote ZIP archives over HTTP - without
downloading the whole thing.

A [zip wire](https://en.wikipedia.org/wiki/Zip-line) gets you straight to your
destination. This library does the same: it uses HTTP range requests to fetch
only the central directory and the specific entries you ask for, skipping
everything else. A 10 KB file inside a 2 GB archive? zipwire downloads roughly
10 KB (plus a small overhead for the central directory), not 2 GB.

## How it works

ZIP archives store a central directory at the end of the file that lists every
entry with its offset and size. zipwire fetches that directory first (a single
range request), then makes one additional range request per file you extract.
The server must support `Range` requests (`Accept-Ranges: bytes`), which most
CDNs, object stores, and static file servers do.

## Key features

- **Selective extraction** - download only the files you need, not the entire
  archive.
- **Streaming decompression** - `read_into` decompresses in chunks, keeping
  memory usage low even for large entries.
- **Sync and async** - `SyncRemoteZip` for synchronous code,
  `AsyncRemoteZip` with `await`/`async with` for asyncio.
- **Wheel metadata** - `SyncRemoteWheel` / `AsyncRemoteWheel` read a Python
  wheel's `.dist-info` (METADATA, WHEEL, RECORD) straight from PyPI in a single
  adaptive tail request, without downloading the wheel.
- **ZIP64** - supports archives and entries larger than 4 GiB.
- **Pluggable backends** - bring your own HTTP library (see below).
- **Local files** - `FileReader` / `AsyncFileReader` open an archive on disk
  through the exact same API, no HTTP server required.
- **S3 objects** - `AiobotocoreReader` reads byte ranges with an existing
  async S3 client, including private buckets and S3-compatible endpoints.

## Installation and backends

The default installation includes the **urllib3** backend. To use a different
HTTP library, install the matching extra - for example httpx2 gives you both
sync and async:

```bash
pip install zipwire[httpx2]
```

| Backend  | Class                | Mode  | HTTP    | Install extra |
|----------|----------------------|-------|---------|---------------|
| urllib3  | `Urllib3Reader`      | sync  | 1.1     | *(included)*  |
| httpx2   | `Httpx2SyncReader`   | sync  | 1.1, 2  | `httpx2`      |
| httpx2   | `Httpx2AsyncReader`  | async | 1.1, 2  | `httpx2`      |
| requests | `RequestsReader`     | sync  | 1.1     | `requests`    |
| aiohttp  | `AiohttpReader`      | async | 1.1     | `aiohttp`     |
| aiobotocore | `AiobotocoreReader` | async | S3 API | `aiobotocore` |

Every HTTP backend accepts an optional pre-configured client or session so you
can share connection pools, authentication, and retry configuration.

`AiobotocoreReader` requires a caller-owned aiobotocore S3 client. Configure
credentials, endpoints, retries, and HTTP transport on that client, then
pass it with the bucket and key:

```python
from aiobotocore.session import get_session

from zipwire import AsyncRemoteZip
from zipwire.backends import AiobotocoreReader

async with get_session().create_client("s3") as client:
    reader = AiobotocoreReader("my-bucket", "archives/data.zip", client=client)
    async with AsyncRemoteZip(reader) as rz:
        data = await rz.read("file.txt")
```

Range requests use the object's ETag to detect changes during extraction.
Pass `extra_args={"VersionId": "..."}` to select a specific object version.

For archives that already live on the local filesystem, `FileReader` and
`AsyncFileReader` (in `zipwire.backends`, no extra dependency) satisfy the same
reader protocols - see the local-file example below.

## Examples

### Read Python wheel metadata without downloading the wheel

A common use case: fetch a wheel's `METADATA`, `WHEEL`, or `RECORD` from PyPI
without downloading the (often huge) wheel itself. `SyncRemoteWheel` and
`AsyncRemoteWheel` parse the wheel URL to locate the `.dist-info` directory and
fetch an adaptive tail, so metadata entries are served from memory without
extra HTTP requests:

```python
from zipwire import SyncRemoteWheel
from zipwire.backends import Urllib3Reader

url = "https://files.pythonhosted.org/.../requests-2.32.3-py3-none-any.whl"
with SyncRemoteWheel(Urllib3Reader(url)) as whl:
    print(whl.read(whl.metadata_name).decode())
```

This optimization relies on the [recommended wheel layout](https://packaging.python.org/en/latest/specifications/binary-distribution-format/#recommended-archiver-features)
of placing `.dist-info` at the end of the archive; wheels built otherwise still
work, falling back to a normal range request per entry.

### Sync - list files and read one

```python
from zipwire import SyncRemoteZip
from zipwire.backends import Urllib3Reader

reader = Urllib3Reader("https://archive.example/data.zip")
with SyncRemoteZip(reader) as rz:
    for info in rz.infolist():
        print(f"{info.filename}  {info.file_size} bytes")

    data = rz.read("path/to/file.txt")
```

### Sync - stream a large file to disk

`read_into` decompresses in chunks so peak memory stays low:

```python
from zipwire import SyncRemoteZip
from zipwire.backends import Urllib3Reader

reader = Urllib3Reader("https://archive.example/large.zip")
with SyncRemoteZip(reader) as rz:
    with open("output.bin", "wb") as f:
        rz.read_into("big-file.bin", f)
```

### Async

```python
import asyncio
from zipwire import AsyncRemoteZip
from zipwire.backends import AiohttpReader

async def main():
    reader = AiohttpReader("https://archive.example/data.zip")
    async with AsyncRemoteZip(reader) as rz:
        data = await rz.read("path/to/file.txt")
        print(data.decode())

asyncio.run(main())
```

### Local archive - same API, no HTTP

`FileReader` opens an archive from disk through the same interface, accepting a
path or a `file://` URI. `AsyncFileReader` is the async counterpart. This lets
code that already uses zipwire handle local and remote archives the same way,
without special-casing either.

```python
from zipwire import SyncRemoteZip
from zipwire.backends import FileReader

with SyncRemoteZip(FileReader("/path/to/archive.zip")) as rz:
    data = rz.read("path/to/file.txt")
```

## License

Apache-2.0
