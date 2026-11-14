"""Incremental decryption for the traditional ZIP encryption format."""

from __future__ import annotations


def _crc_table() -> tuple[int, ...]:
    values = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0xEDB88320 if value & 1 else 0)
        values.append(value)
    return tuple(values)


_CRC_TABLE = _crc_table()


class ZipCrypto:
    """Keep the three ZipCrypto keys across successive compressed chunks."""

    def __init__(self, password: bytes) -> None:
        self._key0 = 0x12345678
        self._key1 = 0x23456789
        self._key2 = 0x34567890
        for value in password:
            self._update(value)

    def _update(self, value: int) -> None:
        self._key0 = (self._key0 >> 8) ^ _CRC_TABLE[(self._key0 ^ value) & 0xFF]
        self._key1 = ((self._key1 + (self._key0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        self._key2 = (self._key2 >> 8) ^ _CRC_TABLE[(self._key2 ^ (self._key1 >> 24)) & 0xFF]

    def decrypt(self, data: bytes) -> bytes:
        output = bytearray(len(data))
        # Keep per-byte state local; publish it once for the next chunk.
        key0, key1, key2 = self._key0, self._key1, self._key2
        table = _CRC_TABLE
        for index, value in enumerate(data):
            temp = key2 | 2
            plain = value ^ (((temp * (temp ^ 1)) >> 8) & 0xFF)
            key0 = (key0 >> 8) ^ table[(key0 ^ plain) & 0xFF]
            key1 = ((key1 + (key0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
            key2 = (key2 >> 8) ^ table[(key2 ^ (key1 >> 24)) & 0xFF]
            output[index] = plain
        self._key0, self._key1, self._key2 = key0, key1, key2
        return bytes(output)
