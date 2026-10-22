"""Frame decoding for the csi-link firmware (firmware/caselink/main/frame_out.h).

Wire format:  0xA5 0x5A | type u8 | length u16 LE | payload | crc16 u16 LE
crc16 is CRC-16/CCITT-FALSE over type, length and payload.

Only the framing is here; CSI payload parsing (numpy) stays out so board
control needs nothing beyond pyserial.
"""

from dataclasses import dataclass

FRAME_MAGIC = b"\xa5\x5a"
FRAME_HEADER_SIZE = 5
FRAME_TRAILER_SIZE = 2
FRAME_MAX_PAYLOAD = 1024

FRAME_TYPE_TEXT = 1
FRAME_TYPE_CSI = 2
FRAME_TYPE_STATUS = 3


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte << 8

        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    return crc


@dataclass
class Frame:
    frame_type: int
    payload: bytes
    raw: bytes  # the whole frame as received, header and crc included


class FrameDecoder:
    """Turns a byte stream into frames, skipping anything between frames
    (boot ROM text, panic dumps, corrupted frames)."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.skipped_bytes = 0
        self.crc_errors = 0
        self.free_text = bytearray()  # non-frame bytes, kept for boot/panic messages

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames = []

        while True:
            start = self._buffer.find(FRAME_MAGIC)

            if start < 0:
                keep = 1 if self._buffer.endswith(FRAME_MAGIC[:1]) else 0
                self._skip(len(self._buffer) - keep)
                return frames

            if start > 0:
                self._skip(start)

            if len(self._buffer) < FRAME_HEADER_SIZE:
                return frames

            frame_type = self._buffer[2]
            length = self._buffer[3] | (self._buffer[4] << 8)

            if length > FRAME_MAX_PAYLOAD:
                self._skip(1)
                continue

            total = FRAME_HEADER_SIZE + length + FRAME_TRAILER_SIZE

            if len(self._buffer) < total:
                return frames

            raw = bytes(self._buffer[:total])
            expected = raw[-2] | (raw[-1] << 8)
            actual = crc16_ccitt(raw[2:-2])

            if actual != expected:
                self.crc_errors += 1
                self._skip(1)
                continue

            payload = raw[FRAME_HEADER_SIZE:-FRAME_TRAILER_SIZE]
            frames.append(Frame(frame_type, payload, raw))
            del self._buffer[:total]

    def _skip(self, count: int) -> None:
        if count <= 0:
            return

        self.free_text.extend(self._buffer[:count])
        del self._buffer[:count]
        self.skipped_bytes += count

    def take_free_text(self) -> str:
        text = self.free_text.decode("utf-8", errors="replace")
        self.free_text.clear()
        return text
