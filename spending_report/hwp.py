"""HWP 5.0 문서에서 표를 읽는다. 외부 패키지 없이 OLE 컨테이너와 본문 레코드만 해석한다.

FF 양식의 값은 모두 표 안에 있으므로 셀 위치와 글자만 꺼낸다. 서식, 그림, 표 밖의 문단은 쓰지 않는다.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END_OF_CHAIN, _FREE = 0xFFFFFFFE, 0xFFFFFFFF
_NO_STREAM = 0xFFFFFFFF

_TAG_PARA_TEXT, _TAG_CTRL_HEADER, _TAG_LIST_HEADER = 67, 71, 72
_CTRL_TABLE = b" lbt"  # 레코드에는 'tbl '이 거꾸로 저장된다
# 글자 대신 8칸을 차지하는 제어 문자
_WIDE_CONTROLS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}


class HwpError(ValueError):
    """HWP로 읽을 수 없는 파일."""


def _ole_streams(data: bytes) -> dict[str, bytes]:
    """OLE 복합 문서의 스트림을 {경로: 내용}으로 돌려준다."""
    if data[:8] != _OLE_MAGIC:
        raise HwpError("OLE 형식이 아님")
    sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
    mini_size = 1 << struct.unpack_from("<H", data, 32)[0]
    dir_start, mini_cutoff, minifat_start = struct.unpack_from("<I", data, 48)[0], *struct.unpack_from("<II", data, 56)
    difat_next, difat_count = struct.unpack_from("<II", data, 68)
    sector = lambda n: data[512 + n * sector_size: 512 + (n + 1) * sector_size]

    fat_sectors = [s for s in struct.unpack_from("<109I", data, 76) if s != _FREE]
    per_difat = sector_size // 4 - 1
    for _ in range(difat_count):
        entries = struct.unpack(f"<{per_difat + 1}I", sector(difat_next))
        fat_sectors += [s for s in entries[:-1] if s != _FREE]
        difat_next = entries[-1]
    fat: list[int] = []
    for s in fat_sectors:
        fat += struct.unpack(f"<{sector_size // 4}I", sector(s))

    def chain(start: int, table: list[int]) -> list[int]:
        out, seen = [], set()
        while start not in (_END_OF_CHAIN, _FREE) and start < len(table) and start not in seen:
            out.append(start)
            seen.add(start)
            start = table[start]
        return out

    directory = b"".join(sector(s) for s in chain(dir_start, fat))
    entries = []
    for i in range(len(directory) // 128):
        raw = directory[i * 128:(i + 1) * 128]
        name_len = struct.unpack_from("<H", raw, 64)[0]
        left, right, child = struct.unpack_from("<III", raw, 68)
        entries.append({"name": raw[:max(name_len - 2, 0)].decode("utf-16-le", "replace"), "type": raw[66],
                        "left": left, "right": right, "child": child,
                        "start": struct.unpack_from("<I", raw, 116)[0], "size": struct.unpack_from("<I", raw, 120)[0]})

    root = entries[0]
    mini_stream = b"".join(sector(s) for s in chain(root["start"], fat))
    mini_fat: list[int] = []
    for s in chain(minifat_start, fat):
        mini_fat += struct.unpack(f"<{sector_size // 4}I", sector(s))

    def read(entry: dict) -> bytes:
        if entry["size"] < mini_cutoff:
            raw = b"".join(mini_stream[s * mini_size:(s + 1) * mini_size] for s in chain(entry["start"], mini_fat))
        else:
            raw = b"".join(sector(s) for s in chain(entry["start"], fat))
        return raw[:entry["size"]]

    streams: dict[str, bytes] = {}

    def walk(index: int, prefix: str, depth: int = 0) -> None:
        if index == _NO_STREAM or index >= len(entries) or depth > 64:
            return
        entry = entries[index]
        walk(entry["left"], prefix, depth + 1)
        path = f"{prefix}{entry['name']}"
        if entry["type"] == 2:
            streams[path] = read(entry)
        elif entry["type"] == 1:
            walk(entry["child"], f"{path}/", depth + 1)
        walk(entry["right"], prefix, depth + 1)

    walk(root["child"], "")
    return streams


def _records(section: bytes):
    pos = 0
    while pos + 4 <= len(section):
        header = struct.unpack_from("<I", section, pos)[0]
        pos += 4
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            size = struct.unpack_from("<I", section, pos)[0]
            pos += 4
        yield tag, level, section[pos:pos + size]
        pos += size


def _para_text(payload: bytes) -> str:
    chars, i, count = [], 0, len(payload) // 2
    codes = struct.unpack(f"<{count}H", payload[:count * 2])
    while i < count:
        code = codes[i]
        if code in _WIDE_CONTROLS:
            i += 8
            continue
        if code >= 32:
            chars.append(chr(code))
        elif code == 9:
            chars.append(" ")
        i += 1
    return "".join(chars)


def _section_tables(section: bytes) -> list[list[list[str]]]:
    tables: list[dict] = []
    stack: list[dict] = []
    for tag, level, payload in _records(section):
        while stack and level <= stack[-1]["level"]:
            stack.pop()
        if tag == _TAG_CTRL_HEADER and payload[:4] == _CTRL_TABLE:
            table = {"level": level, "cells": {}, "current": None}
            tables.append(table)
            stack.append(table)
        elif tag == _TAG_LIST_HEADER and stack and level == stack[-1]["level"] + 1 and len(payload) >= 16:
            col, row = struct.unpack_from("<HH", payload, 8)
            stack[-1]["current"] = (row, col)
            stack[-1]["cells"][(row, col)] = []
        elif tag == _TAG_PARA_TEXT and stack and stack[-1]["current"] is not None:
            stack[-1]["cells"][stack[-1]["current"]].append(_para_text(payload).strip())

    out = []
    for table in tables:
        if not table["cells"]:
            continue
        n_rows = max(r for r, _ in table["cells"]) + 1
        n_cols = max(c for _, c in table["cells"]) + 1
        out.append([["".join(table["cells"].get((r, c), [])).strip() for c in range(n_cols)] for r in range(n_rows)])
    return out


def hwp_tables(path: str | Path) -> list[list[list[str]]]:
    """문서의 모든 표를 행·셀 문자열로 돌려준다. 병합으로 가려진 칸은 빈 문자열이다."""
    streams = _ole_streams(Path(path).read_bytes())
    header = streams.get("FileHeader", b"")
    if not header.startswith(b"HWP Document File"):
        raise HwpError("HWP 문서가 아님")
    properties = struct.unpack_from("<I", header, 36)[0]
    if properties & 0b10:
        raise HwpError("암호가 걸린 문서")
    sections = sorted((k for k in streams if k.startswith("BodyText/Section")), key=lambda k: int(k.rsplit("Section", 1)[1]))
    tables = []
    for name in sections:
        body = streams[name]
        tables += _section_tables(zlib.decompress(body, -15) if properties & 1 else body)
    return tables
