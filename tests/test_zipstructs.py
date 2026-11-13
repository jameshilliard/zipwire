"""Tests for ``zipwire._zipstructs``."""

import dataclasses
import io
import struct
import zipfile

import pytest

from zipwire._constants import MAX_UINT16, MAX_UINT32, ExtraDataId, HeaderFlag
from zipwire._errors import BadZipFile
from zipwire._zipstructs import (
    CentralDirHeaderRecord,
    EOCDRecord,
    ExtraFieldRecord,
    LocalFileHeaderRecord,
    StructRecord,
    Zip64EocdLocatorRecord,
    Zip64EocdRecord,
    Zip64ExtendedInfo,
    find_extra_field,
    iter_extra_fields,
    signature_t,
    uint16_t,
    uint32_t,
    uint64_t,
)

# Record: (struct format, fixed size, MAX_SIZE or None).
EXPECTED_STRUCTS = {
    EOCDRecord: ("<4sHHHHIIH", 22, 65557),
    Zip64EocdLocatorRecord: ("<4sIQI", 20, None),
    Zip64EocdRecord: ("<4sQHHIIQQQQ", 56, None),
    CentralDirHeaderRecord: ("<4sHHHHHHIIIHHHHHII", 46, 196651),
    LocalFileHeaderRecord: ("<4sHHHHHIIIHH", 30, 131100),
    ExtraFieldRecord: ("<HH", 4, 65539),
}

ALL_RECORDS = list(EXPECTED_STRUCTS)


@pytest.fixture
def zip_data() -> bytes:
    """A small in-memory ZIP archive with one deflated entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("hello.txt", b"hello world")
    return buf.getvalue()


@pytest.fixture
def eocd_offset(zip_data: bytes) -> int:
    return len(zip_data) - 22


def corrupt(data: bytes, offset: int, value: bytes) -> bytes:
    """Return *data* with *value* written at *offset* (appended at the end)."""
    mutated = bytearray(data)
    mutated[offset : offset + len(value)] = value
    return bytes(mutated)


@pytest.mark.parametrize("record", ALL_RECORDS)
def test_record_class(record) -> None:
    """Struct, class attributes, and size bounds are as expected."""
    fmt, size, max_size = EXPECTED_STRUCTS[record]
    assert record._STRUCT.format == fmt
    assert record._STRUCT.size == size
    assert size == record.FIXED_SIZE
    names = {field.name for field in dataclasses.fields(record)}
    assert not {"SIGNATURE", "NAME", "MAX_SIZE", "FIXED_SIZE"} & names
    if max_size is not None:
        assert max_size == record.MAX_SIZE
        assert record.MAX_SIZE == record.FIXED_SIZE + record.MAX_VARIABLE_SIZE
        assert size <= record.MAX_SIZE
    else:
        assert not hasattr(record, "MAX_SIZE")


def test_parse_roundtrip(zip_data, eocd_offset) -> None:
    eocd = EOCDRecord.parse(zip_data, eocd_offset)
    assert eocd.entries_total == 1
    assert eocd.comment == b""

    cd = CentralDirHeaderRecord.parse(zip_data, eocd.cd_offset)
    assert cd.filename == b"hello.txt"
    assert cd.decode_filename() == "hello.txt"
    assert cd.compression_method == zipfile.ZIP_DEFLATED
    assert cd.total_size == 46 + cd.filename_length + cd.extra_length + cd.comment_length
    assert cd.date_time >= (1980, 1, 1, 0, 0, 0)

    lfh = LocalFileHeaderRecord.parse(zip_data, cd.header_offset)
    assert lfh.filename == b"hello.txt"
    assert lfh.data_offset_past_header == cd.filename_length + cd.extra_length


def test_parse_rejects_bad_input(zip_data, eocd_offset) -> None:
    with pytest.raises(BadZipFile, match="offset is negative"):
        EOCDRecord.parse(zip_data, -22)
    with pytest.raises(BadZipFile, match="truncated"):
        EOCDRecord.parse(zip_data[: eocd_offset + 10], eocd_offset)


@pytest.mark.parametrize(
    "record", [LocalFileHeaderRecord, CentralDirHeaderRecord, Zip64EocdRecord]
)
def test_bad_signature_rejected(record) -> None:
    """Each record with a required signature rejects a wrong one."""
    with pytest.raises(BadZipFile, match="signature"):
        record.parse(b"XXXX" + bytes(record.FIXED_SIZE - 4))


@pytest.mark.parametrize(
    ("offset", "value", "match"),
    [
        (0, b"XXXX", "signature"),
        (4, struct.pack("<H", 1), "multiple disks"),
        (6, struct.pack("<H", 1), "multiple disks"),
        (20, struct.pack("<H", 100), "extends past end of data"),
    ],
)
def test_eocd_corrupt_rejected(zip_data, eocd_offset, offset, value, match) -> None:
    with pytest.raises(BadZipFile, match=match):
        EOCDRecord.parse(corrupt(zip_data, eocd_offset + offset, value), eocd_offset)


def test_eocd_trailing_data_tolerated(zip_data, eocd_offset) -> None:
    """Data after the EOCD comment is ignored, as in ``zipfile``."""
    record = EOCDRecord.parse(zip_data + b"JUNK", eocd_offset)
    assert record.comment == b""


def test_eocd_comment_roundtrip(zip_data, eocd_offset) -> None:
    comment = b"a comment"
    with_comment = (
        zip_data[:eocd_offset]
        + b"PK\x05\x06"
        + struct.pack("<HHHHIIH", 0, 0, 1, 1, 46, 0, len(comment))
        + comment
    )
    offset = len(with_comment) - 22 - len(comment)
    record = EOCDRecord.parse(with_comment, offset)
    assert record.comment == comment


@pytest.mark.parametrize(
    ("offset", "value", "sentinels"),
    [
        (8, struct.pack("<H", MAX_UINT16), False),  # entries_this_disk alone
        (10, struct.pack("<H", MAX_UINT16), True),  # entries_total
        (12, struct.pack("<I", MAX_UINT32), True),  # cd_size
        (16, struct.pack("<I", MAX_UINT32), True),  # cd_offset
    ],
)
def test_eocd_zip64_sentinels(zip_data, eocd_offset, offset, value, sentinels) -> None:
    record = EOCDRecord.parse(corrupt(zip_data, eocd_offset + offset, value), eocd_offset)
    assert record.has_zip64_sentinels is sentinels


def test_lfh_tolerant_load(zip_data, eocd_offset) -> None:
    """A window ending inside the name leaves the parts as None."""
    eocd = EOCDRecord.parse(zip_data, eocd_offset)
    cd = CentralDirHeaderRecord.parse(zip_data, eocd.cd_offset)
    short = LocalFileHeaderRecord.parse(zip_data[: cd.header_offset + 31], cd.header_offset)
    assert short.filename is None
    assert short.extra is None
    with pytest.raises(BadZipFile, match="not loaded"):
        short.decode_filename()


def make_lfh(flags: int, name: bytes) -> LocalFileHeaderRecord:
    header = b"PK\x03\x04" + struct.pack("<HHHHHIIIHH", 20, flags, 0, 0, 0, 0, 0, 0, len(name), 0)
    return LocalFileHeaderRecord.parse(header + name)


def test_decode_filename() -> None:
    # flag bit 11: UTF-8
    record = make_lfh(HeaderFlag.UTF8_FILENAME, "grüße.txt".encode())
    assert record.decode_filename() == "grüße.txt"
    # otherwise CP437: 0x81 is ü, 0xe1 is ß
    record = make_lfh(0, b"gr\x81\xe1e.txt")
    assert record.decode_filename() == "grüße.txt"
    # metadata_encoding overrides CP437
    record = make_lfh(0, b"caf\xe9.txt")
    assert record.decode_filename(metadata_encoding="latin-1") == "café.txt"
    # invalid UTF-8 under flag bit 11
    record = make_lfh(HeaderFlag.UTF8_FILENAME, b"\xff\xfe.txt")
    with pytest.raises(UnicodeDecodeError):
        record.decode_filename()


@pytest.mark.parametrize(("record_size", "valid"), [(43, False), (44, True)])
def test_zip64_eocd_record_size_validated(record_size, valid) -> None:
    """``record_size`` must cover the fixed part after the size field."""
    header = b"PK\x06\x06" + struct.pack("<Q", record_size) + bytes(48)
    if valid:
        assert Zip64EocdRecord.parse(header).record_size == record_size
    else:
        with pytest.raises(BadZipFile, match="smaller than its fixed part"):
            Zip64EocdRecord.parse(header)


@pytest.mark.parametrize(
    ("disk_with_zip64_eocd", "total_disks", "match"),
    [
        (0, 1, None),
        (1, 1, "multiple disks"),
        (0, 2, "multiple disks"),
    ],
)
def test_zip64_locator_parsed(disk_with_zip64_eocd, total_disks, match) -> None:
    header = b"PK\x06\x07" + struct.pack("<IQI", disk_with_zip64_eocd, 4096, total_disks)
    if match is None:
        record = Zip64EocdLocatorRecord.parse(header)
        assert record.zip64_eocd_offset == 4096
    else:
        with pytest.raises(BadZipFile, match=match):
            Zip64EocdLocatorRecord.parse(header)


def test_zip64_locator_bad_signature() -> None:
    header = b"XXXX" + struct.pack("<IQI", 0, 4096, 1)
    with pytest.raises(BadZipFile, match="signature"):
        Zip64EocdLocatorRecord.parse(header)


def test_subclass_without_width_fields_rejected() -> None:
    with pytest.raises(TypeError, match="no fixed-part fields"):

        class Empty(EOCDRecord):
            pass


def test_non_width_annotations_skipped() -> None:
    """Non-width annotations are skipped silently; they are not runtime-checked."""

    @dataclasses.dataclass(frozen=True, slots=True)
    class Record(StructRecord):
        value: int  # not a width type: ignored
        signature: signature_t
        number: uint16_t

    assert Record._STRUCT.format == "<4sH"
    assert Record.FIXED_SIZE == 6


def test_cdh_variables_past_end_rejected(zip_data, eocd_offset) -> None:
    eocd = EOCDRecord.parse(zip_data, eocd_offset)
    bad = corrupt(zip_data, eocd.cd_offset + 28, struct.pack("<H", 500))  # filename_length
    with pytest.raises(BadZipFile, match="file name"):
        CentralDirHeaderRecord.parse(bad, eocd.cd_offset)


def make_cdh(extra: bytes = b"", **overrides) -> CentralDirHeaderRecord:
    """A parsed central directory header with overridable fields."""
    values: dict[str, int] = {
        "version_made_by": 45,
        "version_needed": 45,
        "flags": 0,
        "compression_method": 8,
        "mod_time": 0,
        "mod_date": 0,
        "crc32": 0,
        "compressed_size": 10,
        "uncompressed_size": 10,
        "filename_length": 0,
        "extra_length": 0,
        "comment_length": 0,
        "disk_start": 0,
        "internal_attr": 0,
        "external_attr": 0,
        "header_offset": 0,
    }
    values.update(overrides)
    values["extra_length"] = len(extra)
    header = b"PK\x01\x02" + struct.pack("<HHHHHHIIIHHHHHII", *values.values())
    return CentralDirHeaderRecord.parse(header + extra)


@pytest.mark.parametrize(
    ("blob", "header_ids"),
    [
        (
            struct.pack("<HH", 0x5455, 4)
            + b"abcd"
            + struct.pack("<HH", ExtraDataId.ZIP64, 0)
            + struct.pack("<HH", 0x7875, 1)
            + b"\x01",
            [0x5455, ExtraDataId.ZIP64, 0x7875],
        ),
        # a trailing partial header (fewer than 4 bytes) is ignored
        (struct.pack("<HH", 0x5455, 2) + b"ab" + b"\x01\x02", [0x5455]),
        (b"", []),
    ],
)
def test_iter_extra_fields(blob, header_ids) -> None:
    assert [f.header_id for f in iter_extra_fields(blob)] == header_ids


def test_iter_extra_fields_overrun_rejected() -> None:
    with pytest.raises(BadZipFile, match="extends past end of data"):
        list(iter_extra_fields(struct.pack("<HH", 0x5455, 100)))


def test_find_extra_field() -> None:
    blob = (
        struct.pack("<HH", 0x5455, 1)
        + b"a"
        + struct.pack("<HH", ExtraDataId.ZIP64, 0)
        + struct.pack("<HH", ExtraDataId.ZIP64, 2)
        + b"zz"
    )
    record = find_extra_field(blob, ExtraDataId.ZIP64)
    assert record is not None and record.data_size == 0
    assert find_extra_field(blob, 0x7875) is None
    assert find_extra_field(b"", ExtraDataId.ZIP64) is None


@pytest.mark.parametrize(
    ("overrides", "payload", "expected"),
    [
        ({}, b"", Zip64ExtendedInfo()),
        (
            {"header_offset": MAX_UINT32},
            struct.pack("<Q", 4096),
            Zip64ExtendedInfo(header_offset=uint64_t(4096)),
        ),
        (
            {
                "uncompressed_size": MAX_UINT32,
                "compressed_size": MAX_UINT32,
                "header_offset": MAX_UINT32,
                "disk_start": MAX_UINT16,
            },
            struct.pack("<QQQI", 123, 456, 789, 2),
            Zip64ExtendedInfo(uint64_t(123), uint64_t(456), uint64_t(789), uint32_t(2)),
        ),
    ],
)
def test_zip64_resolve(overrides, payload, expected) -> None:
    assert Zip64ExtendedInfo.resolve(payload, make_cdh(**overrides)) == expected


@pytest.mark.parametrize(
    ("overrides", "payload", "field"),
    [
        ({"uncompressed_size": MAX_UINT32}, b"", "File size"),
        ({"compressed_size": MAX_UINT32}, b"", "Compress size"),
        ({"header_offset": MAX_UINT32}, b"", "Header offset"),
        ({"disk_start": MAX_UINT16}, b"", "Disk start number"),
        # file size is read first, then compress size finds the end
        (
            {"uncompressed_size": MAX_UINT32, "compressed_size": MAX_UINT32},
            struct.pack("<Q", 1),
            "Compress size",
        ),
    ],
)
def test_zip64_resolve_truncated(overrides, payload, field) -> None:
    with pytest.raises(BadZipFile, match=field):
        Zip64ExtendedInfo.resolve(payload, make_cdh(**overrides))


def test_zip64_resolve_end_to_end() -> None:
    """Walk the extra blob of a parsed header, resolve the sentinels."""
    payload = struct.pack("<Q", 5_000_000_000)
    extra = struct.pack("<HH", ExtraDataId.ZIP64, len(payload)) + payload
    cd = make_cdh(extra=extra, uncompressed_size=MAX_UINT32)
    (field,) = (f for f in iter_extra_fields(cd.extra) if f.header_id == ExtraDataId.ZIP64)
    assert Zip64ExtendedInfo.resolve(field.data, cd).file_size == 5_000_000_000
