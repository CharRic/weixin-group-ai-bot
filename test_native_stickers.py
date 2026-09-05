import json
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch
from bot import Bot
from native_stickers import NativeStickers


class NativeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.ai = Mock(config={'vision_model': 'deepseek-v4-flash-vision-exp'})
        self.ai.complete.return_value = (json.dumps({'stickers': [{'label': '开心小狗', 'row': 1, 'column': 1}]}), {})
        self.ready = Mock()
        self.lib = NativeStickers(self.ai, self.ready)
        self.lib.ui = Mock(return_value={'image': 'AA==', 'header': 'hash', 'geometry': [90, 122, 464, 472]})

    def test_native_search_binds_selection_to_group(self):
        result = self.lib.search('111@chatroom', '开心')
        ident = result['stickers'][0]['id']
        self.ready.assert_called_once_with('111@chatroom')
        self.assertEqual(self.lib.get(ident, '111@chatroom')['point'], [56, 151])
        with self.assertRaises(ValueError):
            self.lib.get(ident, '222@chatroom')
        self.assertEqual(self.ai.complete.call_args.kwargs['model'], 'deepseek-v4-flash-vision-exp')

    def test_new_search_invalidates_previous_selection(self):
        ident = self.lib.search('111@chatroom', '开心')['stickers'][0]['id']
        self.lib.search('111@chatroom', '可爱')
        with self.assertRaises(ValueError):
            self.lib.get(ident, '111@chatroom')

    def test_empty_favorites_returns_no_candidate(self):
        self.ai.complete.return_value = ('{"stickers":[]}', {})
        self.assertEqual(self.lib.search('111@chatroom', '开心', 'favorites')['stickers'], [])

    def test_toolbar_coordinate_from_vision_is_rejected(self):
        self.ai.complete.return_value = ('{"stickers":[{"label":"搜索按钮","row":5,"column":1}]}', {})
        self.assertEqual(self.lib.search('111@chatroom', '开心')['stickers'], [])

    def test_expired_selection_cannot_send(self):
        ident = self.lib.search('111@chatroom', '开心')['stickers'][0]['id']
        self.lib.selections[ident]['prepared'] -= 91
        with self.assertRaises(ValueError):
            self.lib.send(ident, '111@chatroom')


class BoundToolTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot.__new__(Bot)
        self.bot.groups = {'111@chatroom': {'group_id': '111@chatroom'}}
        self.bot.config = {'mode': 'send'}
        self.bot.state = sqlite3.connect(':memory:')
        self.bot.state.row_factory = sqlite3.Row
        self.bot.state.execute('CREATE TABLE sticker_jobs(reply_id INTEGER PRIMARY KEY,group_id TEXT,sticker_id TEXT,status TEXT,created INTEGER)')
        self.bot.stickers = Mock()
        self.bot.stickers.search.return_value = {'stickers': [{'id': 'chosen'}]}
        def send(job):
            self.bot.state.execute("UPDATE sticker_jobs SET status='confirmed' WHERE reply_id=?", (job['reply_id'],))
        self.bot.send_sticker = Mock(side_effect=send)
        self.handle = self.bot.sticker_handler({'id': 1, 'group_id': '111@chatroom'})

    def tearDown(self):
        self.bot.state.close()

    def test_search_then_send_only_to_bound_group(self):
        self.handle('search_stickers', {'query': '开心'})
        self.assertEqual(self.handle('send_sticker', {'sticker_id': 'chosen'})['status'], 'confirmed')
        self.assertEqual(self.bot.send_sticker.call_args.args[0]['group_id'], '111@chatroom')

    def test_model_cannot_override_destination(self):
        self.assertIn('error', self.handle('send_sticker', {'sticker_id': 'chosen', 'group_id': '222@chatroom'}))
        self.bot.send_sticker.assert_not_called()

    def test_cannot_send_unoffered_id(self):
        self.assertIn('error', self.handle('send_sticker', {'sticker_id': 'invented'}))
        self.bot.send_sticker.assert_not_called()

    def test_duplicate_tool_call_does_not_duplicate_delivery(self):
        self.handle('search_stickers', {'query': '开心'})
        for _ in range(2):
            self.handle('send_sticker', {'sticker_id': 'chosen'})
        self.bot.send_sticker.assert_called_once()

    def test_failed_delivery_is_not_claimed_confirmed_or_retried(self):
        self.bot.send_sticker.side_effect = RuntimeError('UI failure')
        self.handle('search_stickers', {'query': '开心'})
        self.assertEqual(self.handle('send_sticker', {'sticker_id': 'chosen'})['status'], 'failed_or_uncertain')
        self.handle('send_sticker', {'sticker_id': 'chosen'})
        self.bot.send_sticker.assert_called_once()
