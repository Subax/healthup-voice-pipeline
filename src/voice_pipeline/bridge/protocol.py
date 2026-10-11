"""Unreal 브리지 메시지 (개발 포인트 문서 §7.5, 설계서 §22 확장 제안).

백엔드 → Unreal
    {"type": "audio_begin", "turn_id", "sample_rate", "channels"}
    바이너리 프레임: [u32 turn_id][u32 seq][u16 flags] + PCM16 (flags bit0 = 마지막 프레임)
    {"type": "audio_end", "turn_id", "last_seq"}
    {"type": "interrupt", "turn_id", "fade_ms"}
    {"type": "avatar_state", "state", "emotion"?, "gesture"?}

Unreal → 백엔드
    {"type": "playback", "turn_id", "event": "started"|"progress"|"stopped"|"finished", "played_ms"}

PCM은 바이너리로 보낸다. base64는 크기가 약 33% 늘어난다.
"""

from __future__ import annotations

import struct

HEADER = struct.Struct("<IIH")
FLAG_LAST = 0x1


def pack_frame(turn_id: int, seq: int, pcm16: bytes, is_last: bool = False) -> bytes:
    return HEADER.pack(turn_id, seq, FLAG_LAST if is_last else 0) + pcm16


def unpack_frame(data: bytes) -> tuple[int, int, bool, bytes]:
    turn_id, seq, flags = HEADER.unpack_from(data)
    return turn_id, seq, bool(flags & FLAG_LAST), data[HEADER.size:]
