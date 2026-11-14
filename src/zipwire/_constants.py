"""Shared constants: integer bounds, enums, and fetch sizes.

ZIP record signatures, struct formats, and fixed sizes are derived by
the record types in ``zipwire._zipstructs`` and live there.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag

# ---------------------------------------------------------------------------
# Integer bounds and sentinel values of ZIP fields
# ---------------------------------------------------------------------------
MAX_UINT16 = 0xFFFF
MAX_UINT32 = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# Compression methods
# ---------------------------------------------------------------------------


class CompressionMethod(IntEnum):
    """ZIP compression method identifiers."""

    STORED = 0  # zipfile.ZIP_STORED
    DEFLATED = 8  # zipfile.ZIP_DEFLATED
    BZIP2 = 12  # zipfile.ZIP_BZIP2
    LZMA = 14  # zipfile.ZIP_LZMA
    ZSTANDARD = 93  # zipfile.ZIP_ZSTANDARD (3.14+)


class HeaderFlag(IntFlag):
    """General purpose bit flags of a file header (APPNOTE 4.4.4).

    Single-bit flags only; the multi-bit compression option field
    (bits 1-2) and the reserved bits are omitted.
    """

    ENCRYPTED = 1 << 0
    HAS_DATA_DESCRIPTOR = 1 << 3
    COMPRESSED_PATCHED_DATA = 1 << 5
    STRONG_ENCRYPTION = 1 << 6
    UTF8_FILENAME = 1 << 11
    MASK_HEADER_VALUES = 1 << 13


class ExtraDataId(IntEnum):
    """Extra data field header ids (APPNOTE 4.5)."""

    ZIP64 = 0x0001
    # AV_INFO = 0x0007
    # EXTENDED_LANGUAGE_ENCODING = 0x0008
    # OS2 = 0x0009
    # NTFS = 0x000A
    # OPENVMS = 0x000C
    # PKWARE_UNIX = 0x000D
    # FILE_STREAM_AND_FORK_DESCRIPTORS = 0x000E
    # PATCH_DESCRIPTOR = 0x000F
    # PKCS7 = 0x0014
    # X509_CERT_ID_AND_SIGNATURE_FOR_FILE = 0x0015
    # X509_CERT_ID_FOR_CENTRAL_DIR = 0x0016
    # STRONG_ENCRYPTION_HEADER = 0x0017
    # RECORD_MANAGEMENT_CONTROLS = 0x0018
    # PKCS7_ENC_RECIPIENT_CERT_LIST = 0x0019
    # TIMESTAMP_RECORD = 0x0020
    # POLICY_DECRYPTION_KEY = 0x0021
    # SMARTCRYPT_KEY_PROVIDER = 0x0022
    # SMARTCRYPT_POLICY_KEY_DATA = 0x0023
    # IBM_S390_UNCOMP = 0x0065
    # IBM_S390_COMP = 0x0066
    # POSZIP_4690 = 0x4690
    # EXTENDED_TIMESTAMP = 0x5455
    # BEOS = 0x6542
    UNICODE_PATH = 0x7075
    # ASI_UNIX = 0x756E
    # INFOZIP_UNIX = 0x7855
    # INFOZIP_UNIX_VAR_SIZE = 0x7875
    # APACHE_COMMONS_COMPRESS = 0xA11E
    # MICROSOFT_OPEN_PACKAGING_GROWTH_HINT = 0xA220
    # SMS_QDOS = 0xFD4A
    # AEX_ENCRYPTION = 0x9901


# ---------------------------------------------------------------------------
# Fetch sizes
# ---------------------------------------------------------------------------
STREAM_CHUNK_SIZE = 2 * 1024 * 1024  # 2 MiB - default for stream_range backends

# For small files, fetch the local file header and compressed data in a
# single request instead of two.  The extra allowance covers the
# variable-length filename and extra fields in the local header.
PREFETCH_THRESHOLD = 50 * 1024  # 50 KiB
PREFETCH_EXTRA = 1024  # generous allowance for filename + extra fields


def range_header(offset: int, length: int) -> str:
    """Return an HTTP Range header value for an absolute byte range."""
    return f"bytes={offset}-{offset + length - 1}"
