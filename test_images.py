import io
import json
import hashlib
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from ai_client import AIClient
from image_context import ImageContext, ImageUnavailable, decode_dat, normalize_image
import test_bot
from bot import table_for


def sample_image():
    out = io.BytesIO()
    Image.new('RGB', (40, 40), 'red').save(out, format='JPEG')
    return out.getvalue()


def encrypted_image(raw, key=b'0123456789abcdef', xor=133):
    n, tail = 128, 20
    pad = padding.PKCS7(128).padder()
    padded = pad.update(raw[:n]) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (b'\x07\x08V2\x08\x07' + struct.pack('<II', n, tail) + b'\x01' +
            enc.update(padded) + enc.finalize() + raw[n:-tail] + bytes(b ^ xor for b in raw[-tail:]))


class DecoderTests(unittest.TestCase):
    def test_v2_full_image_and_inferred_jpeg_tail(self):
        raw = sample_image()
        self.assertEqual(decode_dat(encrypted_image(raw), b'0123456789abcdef', 133), raw)
        self.assertEqual(decode_dat(encrypted_image(raw), b'0123456789abcdef'), raw)

    def test_wrong_key_and_truncated_file_fail_closed(self):
        for data, key in [(encrypted_image(sample_image()), b'bad-key-xxxxxxxx'),
                          (encrypted_image(sample_image())[:45], b'0123456789abcdef')]:
            with self.assertRaises(ImageUnavailable):
                decode_dat(data, key, 133)

    def test_normalization_decodes_and_limits_image(self):
        data, mime = normalize_image(sample_image())
        self.assertEqual(mime, 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(data)).size, (40, 40))
        with self.assertRaises(ImageUnavailable):
            normalize_image(b'not an image')


class ModelRoutingTests(unittest.TestCase):
    def test_image_uses_vision_then_text_uses_flash_without_image_parts(self):
        ai = AIClient.__new__(AIClient)
        ai.config = {'model': 'deepseek-v4-flash', 'vision_model': 'deepseek-v4-flash-vision-exp'}
        ai.request = Mock(return_value={'choices': [{'message': {'content': 'red square'}}]})
        description, _ = ai.describe_image(sample_image(), 'image/jpeg')
        vision = ai.request.call_args.args[1]
        ai.complete([{'role': 'user', 'content': description}])
        text = ai.request.call_args.args[1]
        self.assertEqual(vision['model'], 'deepseek-v4-flash-vision-exp')
        self.assertIn('image_url', json.dumps(vision))
        self.assertEqual(text['model'], 'deepseek-v4-flash')
        self.assertNotIn('image_url', json.dumps(text))
        self.assertEqual(ai.config['model'], 'deepseek-v4-flash')


class ImageStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.account = self.root / 'data/wechat/xwechat_files/test_account'
        self.account.mkdir(parents=True)
        self.db = sqlite3.connect(':memory:')
        self.ai = Mock()
        self.ai.describe_image.return_value = ('a red square', {})
        self.images = ImageContext(self.root, self.db, self.ai, 'wxid_test')
        self.item = {'local_id': 1, 'create_time': 1788520000, 'packed_info_data': b'a' * 32}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_file_lookup_and_cache_do_not_cross_groups(self):
        group = '111@chatroom'
        folder = self.account / 'msg/attach' / hashlib.md5(group.encode()).hexdigest() / '2026-09/Img'
        folder.mkdir(parents=True)
        (folder / ('a' * 32 + '.dat')).write_bytes(sample_image())
        result = self.images.describe(self.account, group, 'message/0', self.item)
        self.assertEqual(result, 'a red square')
        self.images.describe(self.account, group, 'message/0', self.item)
        self.ai.describe_image.assert_called_once()
        self.assertIsNone(self.images.cached('222@chatroom', 'message/0', 1))
        with self.assertRaises(ImageUnavailable):
            self.images.describe(self.account, '222@chatroom', 'message/0', self.item)

    def test_kvcomm_key_is_verified_against_ciphertext(self):
        kv = self.root / 'data/wechat/.xwechat/net/kvcomm'
        kv.mkdir(parents=True)
        (kv / 'key_123456_0_test.statistic').touch()
        key = hashlib.md5(b'123456wxid_test').hexdigest()[:16].encode()
        data = encrypted_image(sample_image(), key, 123456 & 255)
        self.assertEqual(self.images.keys(self.account, data), (key, 123456 & 255))
        with self.assertRaises(ImageUnavailable):
            self.images.keys(self.account, encrypted_image(sample_image()))


class ImageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bot.ContextTests()
        self.fixture.setUp()
        self.bot = self.fixture.bot
        self.bot.images = Mock()
        self.bot.images.describe.return_value = 'Only A picture'
        self.bot.images.cached.return_value = None
        self.c = self.fixture.connections['message/message_0.db']
        self.t = table_for('111@chatroom')
        self.c.execute('UPDATE '+self.t+' SET local_type=3 WHERE local_id IN (13,15,21)')

    def tearDown(self):
        self.fixture.tearDown()

    def test_nearby_images_are_same_sender_same_group_and_before_question(self):
        result = self.bot.image_context_for(self.fixture.trigger)
        self.assertIn('Only A picture', result)
        self.bot.images.describe.assert_called_once()
        args = self.bot.images.describe.call_args.args
        self.assertEqual(args[1], '111@chatroom')
        self.assertEqual(args[3]['local_id'], 13)

    def test_reference_cannot_load_other_group(self):
        xml = '<msg><appmsg><refermsg><type>3</type><fromusr>222@chatroom</fromusr><svrid>13</svrid></refermsg></appmsg></msg>'
        self.c.execute('UPDATE '+self.t+' SET local_type=?,message_content=? WHERE local_id=20', ((57<<32)|49, xml))
        result = self.bot.image_context_for(self.fixture.trigger)
        self.assertIn('不属于当前群', result)
        self.bot.images.describe.assert_not_called()

    def test_reference_to_older_image_from_another_member_in_same_group(self):
        xml = '<msg><appmsg><refermsg><type>3</type><fromusr>111@chatroom</fromusr><svrid>15</svrid></refermsg></appmsg></msg>'
        self.c.execute('UPDATE '+self.t+' SET local_type=?,message_content=? WHERE local_id=20', ((57<<32)|49, xml))
        self.bot.image_context_for(self.fixture.trigger)
        self.assertEqual(self.bot.images.describe.call_args.args[3]['local_id'], 15)

    def test_final_flash_payload_contains_description_and_no_image_data(self):
        messages, _ = self.bot.messages_for(self.fixture.trigger)
        payload = json.dumps(messages)
        self.assertIn('Only A picture', payload)
        self.assertNotIn('B-private', payload)
        self.assertNotIn('image_url', payload)

    def set_reference(self, source, server_id, chat=''):
        xml = ('<msg><appmsg><refermsg><type>3</type><fromusr>' + source +
            '</fromusr><chatusr>' + chat + '</chatusr><svrid>' + str(server_id) +
            '</svrid></refermsg></appmsg></msg>')
        self.c.execute('UPDATE '+self.t+' SET local_type=?,message_content=? WHERE local_id=20', ((57<<32)|49, xml))

    def test_sender_based_reference_resolves_same_group(self):
        self.set_reference('wxid_a', 13)
        self.assertIn('Only A picture', self.bot.image_context_for(self.fixture.trigger))
        args = self.bot.images.describe.call_args.args
        self.assertEqual((args[1], args[3]['local_id']), ('111@chatroom', 13))

    def test_sender_based_reference_can_quote_other_member(self):
        self.set_reference('wxid_bot', 15)
        self.assertIn('Only A picture', self.bot.image_context_for(self.fixture.trigger))
        self.assertEqual(self.bot.images.describe.call_args.args[3]['local_id'], 15)

    def test_sender_mismatch_does_not_fall_back_to_nearby_image(self):
        self.set_reference('wxid_b', 13)
        self.assertIn('未在当前群', self.bot.image_context_for(self.fixture.trigger))
        self.bot.images.describe.assert_not_called()

    def test_sender_reference_to_other_room_server_id_is_not_read(self):
        self.c.execute('UPDATE '+table_for('222@chatroom')+' SET local_type=3 WHERE server_id=113')
        self.set_reference('wxid_b', 113)
        self.assertIn('未在当前群', self.bot.image_context_for(self.fixture.trigger))
        self.bot.images.describe.assert_not_called()

    def test_explicit_other_chat_is_rejected_for_sender_reference(self):
        self.set_reference('wxid_a', 13, '222@chatroom')
        self.assertIn('不属于当前群', self.bot.image_context_for(self.fixture.trigger))
        self.bot.images.describe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
