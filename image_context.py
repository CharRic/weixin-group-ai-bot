"""Local group-scoped image decoding and cached vision descriptions.

V1/V2 format: 15-byte header, PKCS7 AES-ECB region, raw region, XOR tail.
Never download URLs from message XML or search another conversation's files.
"""
import hashlib
import io
import json
import re
import struct
import warnings
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image


MAX_BYTES = 12 * 1024 * 1024


class ImageUnavailable(RuntimeError):
    pass


def is_image(data):
    return (data.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF8', b'wxgf'))
            or (data.startswith(b'RIFF') and data[8:12] == b'WEBP'))


def decode_dat(data, key=None, xor_key=None):
    if not data or len(data) > MAX_BYTES:
        raise ImageUnavailable('图片为空或超过大小限制')
    if is_image(data):
        return data
    if data[:6] in (b'\x07\x08V1\x08\x07', b'\x07\x08V2\x08\x07'):
        if data[:6] == b'\x07\x08V1\x08\x07':
            key = b'cfcd208495d565ef'
        if key is None or len(key) != 16 or len(data) < 31:
            raise ImageUnavailable('本机图片解密密钥尚未就绪')
        aes_size, xor_size = struct.unpack_from('<II', data, 6)
        encrypted_size = (aes_size // 16 + 1) * 16
        end = 15 + encrypted_size
        if end > len(data) or xor_size > len(data) - end:
            raise ImageUnavailable('图片文件不完整')
        try:
            decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            padded = decoder.update(data[15:end]) + decoder.finalize()
            unpad = padding.PKCS7(128).unpadder()
            head = unpad.update(padded) + unpad.finalize()
        except ValueError:
            raise ImageUnavailable('图片解密校验失败') from None
        if len(head) != aes_size or not is_image(head):
            raise ImageUnavailable('图片密钥与当前文件不匹配')
        raw_end = len(data) - xor_size
        if xor_size and xor_key is None:
            if head.startswith(b'\xff\xd8\xff') and xor_size >= 2:
                candidate = data[-2] ^ 0xff
                if data[-1] ^ candidate == 0xd9:
                    xor_key = candidate
            if xor_key is None:
                raise ImageUnavailable('图片尾部解密密钥缺失')
        tail = bytes(value ^ xor_key for value in data[raw_end:]) if xor_size else b''
        return head + data[end:raw_end] + tail
    for magic in (b'\xff\xd8\xff', b'\x89PNG', b'GIF8', b'RIFF'):
        k = data[0] ^ magic[0]
        if bytes(b ^ k for b in data[:len(magic)]) == magic:
            return bytes(b ^ k for b in data)
    raise ImageUnavailable('无法识别本机图片格式')


def normalize_image(data):
    """Decode fully, strip metadata and bound the upload size/resolution."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > 25_000_000:
                    raise ImageUnavailable('图片像素数超过限制')
                im.load()
                im.thumbnail((1600, 1600))
                output = io.BytesIO()
                im.convert('RGB').save(output, format='JPEG', quality=88)
                return output.getvalue(), 'image/jpeg'
    except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageUnavailable('图片尚不完整或格式暂不支持') from None


class ImageContext:
    def __init__(self, root, state, ai, bot_id):
        self.root, self.state, self.ai, self.bot_id = Path(root), state, ai, bot_id
        state.execute('''CREATE TABLE IF NOT EXISTS image_memory(
            group_id TEXT, shard TEXT, local_id INTEGER, image_hash TEXT, summary TEXT,
            PRIMARY KEY(group_id,shard,local_id))''')
        state.commit()

    def cached(self, group_id, shard, local_id):
        row = self.state.execute('SELECT summary FROM image_memory WHERE group_id=? AND shard=? AND local_id=?',
                                 (group_id, shard, local_id)).fetchone()
        return row[0] if row else None

    def keys(self, account, sample):
        key_path = self.root / 'image_keys.json'
        if key_path.exists():
            cfg = json.loads(key_path.read_text())
            if cfg.get('account') == account.name:
                try:
                    key = cfg['aes_key'].encode('ascii')
                    decode_dat(sample, key, cfg.get('xor_key'))
                    return key, cfg.get('xor_key')
                except (ValueError, KeyError, ImageUnavailable):
                    pass
        # Linux client versions using kvcomm-derived account image keys.
        # Verify each candidate against this file; never assume derivation works.
        kvcomm = self.root / 'data/wechat/.xwechat/net/kvcomm'
        for path in kvcomm.glob('*.statistic'):
            for value in path.name.removeprefix('key_').split('_')[:2]:
                if not value.isdigit():
                    continue
                for wxid in (self.bot_id, account.name):
                    key = hashlib.md5((value + wxid).encode()).hexdigest()[:16].encode()
                    xor = int(value) & 255
                    try:
                        decode_dat(sample, key, xor)
                        return key, xor
                    except ImageUnavailable:
                        continue
        raise ImageUnavailable('当前账号的图片解密密钥尚未就绪')

    def paths(self, account, group_id, item):
        if not re.fullmatch(r'[0-9]+@chatroom', group_id):
            raise ImageUnavailable('无效图片会话')
        packed = item.get('packed_info_data') or b''
        if isinstance(packed, str):
            packed = packed.encode()
        hashes = list(dict.fromkeys(m.decode() for m in re.findall(rb'[0-9a-f]{32}', packed)))
        group_hash = hashlib.md5(group_id.encode()).hexdigest()
        month = datetime.fromtimestamp(item['create_time']).strftime('%Y-%m')
        folder = account / 'msg/attach' / group_hash / month / 'Img'
        bubble = account / 'cache' / month / 'Message' / group_hash / 'Bubble'
        for file_hash in hashes:
            for path in (folder / (file_hash + '.dat'), folder / (file_hash + '_h.dat'),
                         bubble / (file_hash + '_b.dat'), folder / (file_hash + '_t.dat')):
                if path.is_file() and path.resolve().is_relative_to(account.resolve()):
                    yield path

    def describe(self, account, group_id, shard, item):
        cached = self.cached(group_id, shard, item['local_id'])
        if cached:
            return cached
        last_error = ImageUnavailable('这张图片还没有下载到本机')
        for path in self.paths(account, group_id, item):
            try:
                with path.open('rb') as f:
                    data = f.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ImageUnavailable('图片超过大小限制')
                key, xor = self.keys(account, data) if data[:6] == b'\x07\x08V2\x08\x07' else (None, None)
                image, mime = normalize_image(decode_dat(data, key, xor))
            except (ImageUnavailable, OSError) as exc:
                last_error = exc
                continue
            summary, _ = self.ai.describe_image(image, mime)
            if path.stem.endswith('_t'):
                summary = '（依据缩略图，细节可能不清晰）' + summary
            with self.state:
                self.state.execute('INSERT OR REPLACE INTO image_memory VALUES(?,?,?,?,?)',
                    (group_id, shard, item['local_id'], hashlib.sha256(image).hexdigest(), summary))
            return summary
        if isinstance(last_error, ImageUnavailable):
            raise last_error
        raise ImageUnavailable('本机图片暂时无法读取') from None
