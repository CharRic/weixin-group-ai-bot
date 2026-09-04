"""Read-only snapshots of this project's WeChat SQLCipher databases.

SQLCipher format reference: linux-wechat-agent/tools/wechat-decrypt.
Original databases are never modified. Keys and snapshots remain in the project.
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
import subprocess
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent
PAGE = 4096


def private_json(path, value):
    os.umask(0o077)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(path)


def databases():
    accounts = list((ROOT / 'data/wechat/xwechat_files').glob('*/db_storage'))
    if len(accounts) != 1:
        raise RuntimeError('Expected exactly one logged-in account')
    base = accounts[0]
    paths = [base / 'contact/contact.db', base / 'session/session.db']
    paths += sorted((base / 'message').glob('message_[0-9]*.db'))
    return base, paths


def mac_key(key, salt):
    return hashlib.pbkdf2_hmac('sha512', key, bytes(b ^ 0x3a for b in salt), 2, 32)


def valid_page(page, number, mac):
    offset = 16 if number == 1 else 0
    expected = hmac.new(mac, page[offset:4032] + struct.pack('<I', number), hashlib.sha512).digest()
    return hmac.compare_digest(expected, page[4032:])


def extract_keys():
    base, paths = databases()
    pages = {str(p.relative_to(base)): p.open('rb').read(PAGE) for p in paths}
    remaining = set(pages)
    found = {}
    rows = subprocess.check_output(['docker', 'top', 'weixin-desktop-1', '-eo', 'pid,comm'], text=True).splitlines()[1:]
    pids = [int(row.split()[0]) for row in rows if row.split()[1] == 'wechat']
    if not pids:
        raise RuntimeError('Project WeChat process not found')
    pattern = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
    tried = set()
    for pid in pids:
        if Path(f'/proc/{pid}/comm').read_text().strip() != 'wechat':
            continue
        with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
            for line in Path(f'/proc/{pid}/maps').read_text().splitlines():
                fields = line.split()
                if not fields[1].startswith('rw'):
                    continue
                if len(fields) > 5 and fields[5] != '[heap]':
                    continue
                start, end = (int(s, 16) for s in fields[0].split('-'))
                tail = b''
                for offset in range(start, end, 1024 * 1024):
                    try:
                        mem.seek(offset)
                        chunk = tail + mem.read(min(1024 * 1024, end - offset))
                    except OSError:
                        break
                    for match in pattern.finditer(chunk):
                        key = bytes.fromhex(match[1][:64].decode())
                        if key in tried:
                            continue
                        tried.add(key)
                        for rel in list(remaining):
                            page = pages[rel]
                            if valid_page(page, 1, mac_key(key, page[:16])):
                                found[rel] = key.hex()
                                remaining.remove(rel)
                    tail = chunk[-200:]
                    if not remaining:
                        break
                if not remaining:
                    break
    if remaining:
        raise RuntimeError('Missing database keys for: ' + ', '.join(sorted(remaining)))
    private_json(ROOT / 'database_keys.json', {'account': base.parent.name, 'keys': found})
    print(json.dumps({'database_keys_ready': len(found)}))


def checksum(data, state=(0, 0), endian='<'):
    s0, s1 = state
    words = struct.unpack(endian + str(len(data) // 4) + 'I', data)
    for a, b in zip(words[::2], words[1::2]):
        s0 = (s0 + a + s1) & 0xffffffff
        s1 = (s1 + b + s0) & 0xffffffff
    return s0, s1


def committed_frames(wal):
    if not wal:
        return [], None
    if len(wal) < 32:
        raise RuntimeError('Incomplete WAL header; retry snapshot')
    magic, version, size = struct.unpack('>III', wal[:12])
    if magic not in (0x377f0682, 0x377f0683) or size != PAGE:
        raise RuntimeError('Unsupported WAL format')
    endian = '<' if magic == 0x377f0682 else '>'
    state = checksum(wal[:24], endian=endian)
    if state != struct.unpack('>II', wal[24:32]):
        raise RuntimeError('Invalid WAL header checksum')
    frames, committed, final_size = [], 0, None
    for offset in range(32, len(wal) - (24 + PAGE) + 1, 24 + PAGE):
        header = wal[offset:offset + 24]
        page = wal[offset + 24:offset + 24 + PAGE]
        if header[8:16] != wal[16:24]:
            break
        state = checksum(header[:8] + page, state, endian)
        if state != struct.unpack('>II', header[16:24]):
            break
        number, db_size = struct.unpack('>II', header[:8])
        if not number:
            break
        frames.append((number, page))
        if db_size:
            committed, final_size = len(frames), db_size
    return frames[:committed], final_size


def stamp(path):
    try:
        s = path.stat()
        return s.st_ino, s.st_size, s.st_mtime_ns
    except FileNotFoundError:
        return None


def snapshot(rel):
    base, allowed = databases()
    source = base / rel
    if source not in allowed:
        raise RuntimeError('Database is outside the message/contact allowlist')
    key_config = json.loads((ROOT / 'database_keys.json').read_text())
    if key_config['account'] != base.parent.name:
        raise RuntimeError('Account changed; reconfigure explicitly')
    key = bytes.fromhex(key_config['keys'][rel])
    wal_path = Path(str(source) + '-wal')
    before = (stamp(source), stamp(wal_path))
    raw = source.read_bytes()
    wal = wal_path.read_bytes() if wal_path.exists() else b''
    if before != (stamp(source), stamp(wal_path)):
        raise RuntimeError('Database changed during snapshot; retry')
    if len(raw) % PAGE or not raw:
        raise RuntimeError('Invalid encrypted database length')
    frames, final_size = committed_frames(wal)
    pages = [raw[i:i + PAGE] for i in range(0, len(raw), PAGE)]
    for number, page in frames:
        while len(pages) < number:
            pages.append(bytes(PAGE))
        pages[number - 1] = page
    if final_size is not None:
        pages = pages[:final_size]
    mac = mac_key(key, raw[:16])
    result = bytearray()
    for number, page in enumerate(pages, 1):
        if page == bytes(PAGE):
            result.extend(page)
            continue
        if not valid_page(page, number, mac):
            raise RuntimeError('Database page authentication failed')
        offset = 16 if number == 1 else 0
        decryptor = Cipher(algorithms.AES(key), modes.CBC(page[4016:4032])).decryptor()
        decoded = decryptor.update(page[offset:4016]) + decryptor.finalize()
        result.extend((b'SQLite format 3\0' if number == 1 else b'') + decoded + bytes(80))
    # Snapshot has its committed WAL merged; use rollback mode in this memory copy.
    result[18:20] = bytes([1, 1])
    connection = sqlite3.connect(':memory:')
    connection.deserialize(bytes(result))
    connection.execute('PRAGMA query_only=ON')
    connection.row_factory = sqlite3.Row
    return connection


if __name__ == '__main__':
    extract_keys()
