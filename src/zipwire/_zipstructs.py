"""Declarative record types for ZIP structures.

Each record is a frozen dataclass whose fixed-part fields are
annotated with a width type (``uint16_t`` and friends).
``StructRecord.parse()`` unpacks the fixed part, checks the signature,
validates, and loads the variable-length parts.
``__init_subclass__`` derives each record's ``_STRUCT`` from the
annotations, so field list and struct format cannot drift apart.

Records whose buffer is contractually complete (the central directory,
extra fields, and the EOCD tail) require their variable parts to fit;
the local file header loads them tolerantly, because readers fetch a
window that may end inside the name.  The EOCD comment must fit the
buffer, but data after it is tolerated, as in ``zipfile``.  Concatenated extra fields are
walked with ``iter_extra_fields()``; the ZIP64 extended information
field is resolved with ``Zip64ExtendedInfo.resolve()`` and the Unicode
Path field with ``UnicodePathExtraField.resolve()``.

Shared integer bounds live in ``zipwire._constants``.  All records
operate on ``bytes`` - no IO.
"""

from __future__ import annotations

import inspect
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, NewType, Self
from zlib import crc32

from zipwire._constants import MAX_UINT16, MAX_UINT32, HeaderFlag
from zipwire._errors import BadZipFile

if TYPE_CHECKING:
    from collections.abc import Iterator


# Field width types, named after the C integer types.  A field
# annotated with one is part of the record's fixed part.
# ``signature_t`` is the leading 4-byte record signature.
signature_t = NewType("signature_t", bytes)
uint16_t = NewType("uint16_t", int)
uint32_t = NewType("uint32_t", int)
uint64_t = NewType("uint64_t", int)

_STRUCT_CODES: dict[object, str] = {
    signature_t: "4s",
    uint16_t: "H",
    uint32_t: "I",
    uint64_t: "Q",
}

# ``record_size`` excludes the signature and size field (APPNOTE 4.3.14.1).
_ZIP64_EOCD_SIZED_PREFIX = struct.calcsize("<4sQ")


def filename_encoding(flags: int, metadata_encoding: str | None = None) -> str:
    """Return the encoding of a file name carried with *flags*.

    UTF-8 when general purpose flag bit 11 is set, otherwise
    *metadata_encoding* or CP437, as in ``zipfile``.  A falsy
    *metadata_encoding* means the CP437 default, matching
    ``zipfile``'s ``metadata_encoding or 'cp437'``.
    """
    if flags & HeaderFlag.UTF8_FILENAME:
        return "utf-8"
    if metadata_encoding:
        return metadata_encoding
    return "cp437"


@dataclass(frozen=True)
class StructRecord:
    """Base class for ZIP records with a fixed-size part.

    Fixed-part fields are annotated with a width type;
    ``__init_subclass__`` derives ``_STRUCT`` from them in declaration
    order.  Records with a leading signature declare its expected
    value as the ``SIGNATURE`` class variable; ``None`` skips the
    check.  Variable-length parts are trailing fields with defaults,
    filled by ``parse()`` via the ``_load_variables()`` hook.
    """

    # Empty slots instead of ``slots=True``; workaround for
    # Python 3.11 + 3.12.
    __slots__ = ()

    NAME: ClassVar[str] = "record"
    _STRUCT: ClassVar[struct.Struct]
    FIXED_SIZE: ClassVar[int]
    SIGNATURE: ClassVar[bytes | None]
    # Max bytes of the variable-length parts; ``None`` when unbounded.
    MAX_VARIABLE_SIZE: ClassVar[int | None] = None
    MAX_SIZE: ClassVar[int]

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Derive ``_STRUCT`` and ``FIXED_SIZE`` from the annotations.

        Annotations (strings under PEP 563) are resolved in this
        module and mapped through ``_STRUCT_CODES``; anything else is
        skipped.  A subclass without width-typed fields is rejected.

        Only the class's own annotations are considered, so records
        must derive directly from ``StructRecord``: fixed parts are
        not inherited from a concrete record.
        """
        super().__init_subclass__(**kwargs)
        codes = []
        for annotation in inspect.get_annotations(cls).values():
            if isinstance(annotation, str):
                annotation = globals().get(annotation)
            # A typo silently drops the field from _STRUCT; not runtime-checked.
            code = _STRUCT_CODES.get(annotation)
            if code is not None:
                codes.append(code)
        if not codes:
            raise TypeError(f"{cls.__name__} has no fixed-part fields")
        cls._STRUCT = struct.Struct("<" + "".join(codes))
        cls.FIXED_SIZE = cls._STRUCT.size
        if cls.MAX_VARIABLE_SIZE is not None:
            cls.MAX_SIZE = cls.FIXED_SIZE + cls.MAX_VARIABLE_SIZE

    @classmethod
    def parse(cls, data: bytes, offset: int = 0) -> Self:
        """Unpack, signature-check, validate, and load a record.

        Raises:
            BadZipFile: If *offset* is negative, or the record is
                truncated or invalid.
        """
        if offset < 0:
            raise BadZipFile(f"{cls.NAME} offset is negative")
        if len(data) - offset < cls._STRUCT.size:
            raise BadZipFile(f"{cls.NAME} is truncated")
        if cls.SIGNATURE is not None and data[offset : offset + 4] != cls.SIGNATURE:
            raise BadZipFile(f"Invalid {cls.NAME} signature")
        record = cls(*cls._STRUCT.unpack_from(data, offset))
        record._validate()
        record._load_variables(data, offset)
        return record

    def _validate(self) -> None:
        """Check this record's own fields for consistency."""

    def _load_variables(self, data: bytes, offset: int) -> None:
        """Load the variable-length parts onto the record in place."""

    def _slice(self, data: bytes, start: int, length: int, what: str) -> bytes:
        """Return *length* bytes of *data* at *start*, bounds-checked."""
        end = start + length
        if end > len(data):
            raise BadZipFile(f"{self.NAME} {what} extends past end of data")
        return data[start:end]

    def _slice_optional(self, data: bytes, start: int, length: int) -> bytes | None:
        """Like ``_slice()``, but ``None`` when the buffer is too short."""
        end = start + length
        if end > len(data):
            return None
        return data[start:end]


@dataclass(frozen=True, slots=True)
class EOCDRecord(StructRecord):
    """End of Central Directory record (APPNOTE 4.3.16, 22 bytes).

    The record is normally the last one in the archive, but data after
    the .ZIP file comment is tolerated, as in ``zipfile``: the comment
    only has to fit inside the buffer passed to ``parse()``, not end
    exactly where the buffer ends.
    """

    signature: signature_t
    disk_num: uint16_t
    disk_cd_start: uint16_t
    entries_this_disk: uint16_t
    entries_total: uint16_t
    cd_size: uint32_t
    cd_offset: uint32_t
    comment_length: uint16_t
    comment: bytes = b""

    NAME = "EOCD record"
    SIGNATURE: ClassVar[bytes] = b"PK\x05\x06"
    MAX_VARIABLE_SIZE: ClassVar[int] = MAX_UINT16  # .ZIP file comment

    @property
    def has_zip64_sentinels(self) -> bool:
        """Whether values point at ZIP64 records.

        Like ``zipfile``, only the total entry count and the central
        directory size and offset are checked; a sentinel in
        ``entries_this_disk`` alone does not indicate ZIP64.
        """
        return (
            self.entries_total == MAX_UINT16
            or self.cd_size == MAX_UINT32
            or self.cd_offset == MAX_UINT32
        )

    def _validate(self) -> None:
        if self.disk_num != 0 or self.disk_cd_start != 0:
            raise BadZipFile("zipfiles that span multiple disks are not supported")

    def _load_variables(self, data: bytes, offset: int) -> None:
        start = offset + self._STRUCT.size
        comment = self._slice(data, start, self.comment_length, "comment")
        object.__setattr__(self, "comment", comment)


@dataclass(frozen=True, slots=True)
class Zip64EocdLocatorRecord(StructRecord):
    """ZIP64 End of Central Directory locator (APPNOTE 4.3.15, 20 bytes).

    The record does not validate ``zip64_eocd_offset``: the caller must
    check that the referenced record lies inside the archive and ends
    where this locator begins.
    """

    signature: signature_t
    disk_with_zip64_eocd: uint32_t
    zip64_eocd_offset: uint64_t
    total_disks: uint32_t

    NAME = "ZIP64 EOCD locator"
    SIGNATURE: ClassVar[bytes] = b"PK\x06\x07"

    def _validate(self) -> None:
        if self.disk_with_zip64_eocd != 0 or self.total_disks > 1:
            raise BadZipFile("zipfiles that span multiple disks are not supported")


@dataclass(frozen=True, slots=True)
class Zip64EocdRecord(StructRecord):
    """ZIP64 End of Central Directory record (APPNOTE 4.3.14, 56 bytes).

    The disk number fields and per-disk entry count are ignored, as in
    ``zipfile._EndRecData64``; multidisk archives are rejected via the
    EOCD record and the locator.

    ``record_size`` bounds the extensible data section but is not
    checked against the buffer.  The caller must verify that the
    central directory ends where this record starts and that the
    record plus its extensible data ends where the locator begins.
    """

    signature: signature_t
    record_size: uint64_t
    version_made_by: uint16_t
    version_needed: uint16_t
    disk_num: uint32_t
    disk_cd_start: uint32_t
    entries_this_disk: uint64_t
    entries_total: uint64_t
    cd_size: uint64_t
    cd_offset: uint64_t

    NAME = "ZIP64 EOCD record"
    SIGNATURE: ClassVar[bytes] = b"PK\x06\x06"

    def _validate(self) -> None:
        minimum = self.FIXED_SIZE - _ZIP64_EOCD_SIZED_PREFIX
        if self.record_size < minimum:
            raise BadZipFile(f"{self.NAME} size {self.record_size} is smaller than its fixed part")


@dataclass(frozen=True, slots=True)
class CentralDirHeaderRecord(StructRecord):
    """Central directory file header (APPNOTE 4.3.12, 46 bytes).

    ``compressed_size``, ``uncompressed_size``, ``disk_start``, and
    ``header_offset`` may hold max uint16/32 ZIP64 sentinels;
    resolving them from the extra field is the caller's job.
    """

    signature: signature_t
    version_made_by: uint16_t
    version_needed: uint16_t
    flags: uint16_t
    compression_method: uint16_t
    mod_time: uint16_t
    mod_date: uint16_t
    crc32: uint32_t
    compressed_size: uint32_t
    uncompressed_size: uint32_t
    filename_length: uint16_t
    extra_length: uint16_t
    comment_length: uint16_t
    disk_start: uint16_t
    internal_attr: uint16_t
    external_attr: uint32_t
    header_offset: uint32_t

    filename: bytes = b""
    extra: bytes = b""
    comment: bytes = b""

    NAME = "central directory header"
    SIGNATURE: ClassVar[bytes] = b"PK\x01\x02"
    MAX_VARIABLE_SIZE: ClassVar[int] = 3 * MAX_UINT16  # name, extra, comment

    @property
    def total_size(self) -> int:
        """Size of the fixed part plus the variable-length parts."""
        return self._STRUCT.size + self.filename_length + self.extra_length + self.comment_length

    @property
    def date_time(self) -> tuple[int, int, int, int, int, int]:
        """DOS date and time as a ``ZipInfo.date_time`` tuple.

        Seconds have 2-second resolution; the epoch is 1980.
        """
        return (
            ((self.mod_date >> 9) & 0x7F) + 1980,  # year
            (self.mod_date >> 5) & 0x0F,  # month
            self.mod_date & 0x1F,  # day
            (self.mod_time >> 11) & 0x1F,  # hour
            (self.mod_time >> 5) & 0x3F,  # minute
            (self.mod_time & 0x1F) * 2,  # second
        )

    @property
    def create_system(self) -> int:
        """Upper byte of ``version_made_by``: 0 is MS-DOS, 3 is Unix."""
        return (self.version_made_by >> 8) & 0xFF

    @property
    def create_version(self) -> int:
        """Lower byte of ``version_made_by``, e.g. 45 means 4.5."""
        return self.version_made_by & 0xFF

    def decode_filename(self, *, metadata_encoding: str | None = None) -> str:
        """Decode the file name.

        UTF-8 if flag bit 11 is set, else *metadata_encoding* or
        CP437, as in ``zipfile``.

        Raises:
            UnicodeDecodeError: If the file name is not valid in the
                encoding.
            BadZipFile: If the file name contains a null byte.  No
                mainstream filesystem or archiver produces one; a
                null byte is a hostile trick, not a name.
        """
        name = self.filename.decode(filename_encoding(self.flags, metadata_encoding))
        if "\0" in name:
            raise BadZipFile(f"{self.NAME} file name contains a null byte")
        return name

    def _load_variables(self, data: bytes, offset: int) -> None:
        start = offset + self._STRUCT.size
        object.__setattr__(
            self, "filename", self._slice(data, start, self.filename_length, "file name")
        )
        object.__setattr__(
            self,
            "extra",
            self._slice(data, start + self.filename_length, self.extra_length, "extra field"),
        )
        object.__setattr__(
            self,
            "comment",
            self._slice(
                data,
                start + self.filename_length + self.extra_length,
                self.comment_length,
                "comment",
            ),
        )


@dataclass(frozen=True, slots=True)
class LocalFileHeaderRecord(StructRecord):
    """Local file header (APPNOTE 4.3.7, 30 bytes).

    The sizes are unreliable: with flag bit 3 they are zero and
    followed by a data descriptor, and ZIP64 stores them in an extra
    field - the central directory is the authority.  The variable
    parts load tolerantly: ``None`` when the fetched window ends
    inside them.
    """

    signature: signature_t
    version_needed: uint16_t
    flags: uint16_t
    compression_method: uint16_t
    mod_time: uint16_t
    mod_date: uint16_t
    crc32: uint32_t
    compressed_size: uint32_t
    uncompressed_size: uint32_t
    filename_length: uint16_t
    extra_length: uint16_t

    filename: bytes | None = None
    extra: bytes | None = None

    NAME = "local file header"
    SIGNATURE: ClassVar[bytes] = b"PK\x03\x04"
    MAX_VARIABLE_SIZE: ClassVar[int] = 2 * MAX_UINT16  # name and extra

    @property
    def data_offset_past_header(self) -> int:
        """Number of bytes after the fixed header to skip to reach file data."""
        return self.filename_length + self.extra_length

    def decode_filename(self, *, metadata_encoding: str | None = None) -> str:
        """Decode the file name.

        UTF-8 if flag bit 11 is set, else *metadata_encoding* or
        CP437, as in ``zipfile``.

        Raises:
            BadZipFile: If the file name is not loaded or contains a
                null byte.
        """
        if self.filename is None:
            raise BadZipFile(f"{self.NAME} file name is not loaded")
        name = self.filename.decode(filename_encoding(self.flags, metadata_encoding))
        if "\0" in name:
            raise BadZipFile(f"{self.NAME} file name contains a null byte")
        return name

    def _load_variables(self, data: bytes, offset: int) -> None:
        start = offset + self._STRUCT.size
        object.__setattr__(
            self, "filename", self._slice_optional(data, start, self.filename_length)
        )
        object.__setattr__(
            self,
            "extra",
            self._slice_optional(data, start + self.filename_length, self.extra_length),
        )


@dataclass(frozen=True, slots=True)
class ExtraFieldRecord(StructRecord):
    """Extra data field (APPNOTE 4.5.2).

    The fixed part is the header id and data size; the payload is
    loaded by ``parse()``.  Extra fields have no signature.
    """

    header_id: uint16_t
    data_size: uint16_t
    data: bytes = b""

    NAME = "extra field"
    SIGNATURE: ClassVar[bytes | None] = None
    MAX_VARIABLE_SIZE: ClassVar[int] = MAX_UINT16  # payload

    @property
    def total_size(self) -> int:
        """Size of the header plus the payload."""
        return self._STRUCT.size + self.data_size

    def _load_variables(self, data: bytes, offset: int) -> None:
        start = offset + self._STRUCT.size
        object.__setattr__(self, "data", self._slice(data, start, self.data_size, "data"))


def iter_extra_fields(extra: bytes) -> Iterator[ExtraFieldRecord]:
    """Yield the extra data fields of an extra field blob.

    A trailing partial header (fewer than 4 bytes) is ignored, as in
    ``zipfile``; a payload that overruns the blob raises
    ``BadZipFile``.
    """
    offset = 0
    while offset + 4 <= len(extra):
        record = ExtraFieldRecord.parse(extra, offset)
        yield record
        offset += record.total_size


def find_extra_field(extra: bytes, header_id: int) -> ExtraFieldRecord | None:
    """Return the first extra data field with *header_id*, if any."""
    for record in iter_extra_fields(extra):
        if record.header_id == header_id:
            return record
    return None


@dataclass(frozen=True, slots=True)
class Zip64ExtendedInfo:
    """ZIP64 extended information extra field (APPNOTE 4.5.3).

    A value is present only when the matching field of the central
    directory header holds its sentinel; the values appear in the
    payload in this order.
    """

    file_size: uint64_t | None = None
    compress_size: uint64_t | None = None
    header_offset: uint64_t | None = None
    disk_start: uint32_t | None = None

    @classmethod
    def resolve(cls, data: bytes, cd: CentralDirHeaderRecord) -> Self:
        """Resolve the ZIP64 sentinels of a central directory header.

        Reads the payload of its ZIP64 extended information extra
        field: a value is read only when the matching field of *cd*
        holds its sentinel, in the order file size, compress size,
        header offset, disk start number.

        Raises:
            BadZipFile: If the payload ends before a promised value.
        """
        offset = 0

        def unpack(fmt: str, what: str) -> int:
            nonlocal offset
            size = struct.calcsize(fmt)
            if offset + size > len(data):
                raise BadZipFile(f"Corrupt zip64 extra field. {what} not found.")
            value = struct.unpack_from(fmt, data, offset)[0]
            offset += size
            return value

        file_size: uint64_t | None = None
        compress_size: uint64_t | None = None
        header_offset: uint64_t | None = None
        disk_start: uint32_t | None = None
        if cd.uncompressed_size == MAX_UINT32:
            file_size = uint64_t(unpack("<Q", "File size"))
        if cd.compressed_size == MAX_UINT32:
            compress_size = uint64_t(unpack("<Q", "Compress size"))
        if cd.header_offset == MAX_UINT32:
            header_offset = uint64_t(unpack("<Q", "Header offset"))
        if cd.disk_start == MAX_UINT16:
            disk_start = uint32_t(unpack("<I", "Disk start number"))
        return cls(
            file_size=file_size,
            compress_size=compress_size,
            header_offset=header_offset,
            disk_start=disk_start,
        )


@dataclass(frozen=True, slots=True)
class UnicodePathExtraField:
    """Unicode Path extra field (APPNOTE 4.6.9).

    Carries the UTF-8 form of an entry name whose central directory
    form is legacy-encoded, together with a version and the CRC-32 of
    the raw central directory name that authenticates it.
    """

    name: str

    @classmethod
    def resolve(cls, data: bytes, filename: bytes) -> Self | None:
        """Return the UTF-8 name for *filename*, if the field applies.

        The field is honoured when it is version 1 and its CRC-32
        matches the raw central directory name *filename*, as in
        ``zipfile``.  ``None`` is returned when it does not apply or
        carries an empty name, so the caller keeps the name decoded
        from the central directory.

        Raises:
            BadZipFile: If the payload is too short for the version
                and CRC-32, the name is not valid UTF-8, or the name
                contains a null byte.
        """
        if len(data) < 5:
            raise BadZipFile("Corrupt unicode path extra field (0x7075)")
        version, name_crc = struct.unpack("<BI", data[:5])
        if version != 1 or name_crc != crc32(filename):
            return None
        try:
            name = data[5:].decode("utf-8")
        except UnicodeDecodeError as e:
            raise BadZipFile("Corrupt unicode path extra field (0x7075): invalid utf-8") from e
        if "\0" in name:
            raise BadZipFile("Unicode path extra field (0x7075) name contains a null byte")
        return cls(name) if name else None
