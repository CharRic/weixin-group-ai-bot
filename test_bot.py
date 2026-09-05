import struct
import sqlite3
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch
from bot import Bot, configured_groups, estimate_tokens, prompt_for, table_for
from wechat_db import checksum, committed_frames


class MentionTests(unittest.TestCase):
    def setUp(self):
        self.config = {'bot_id': 'wxid_bot', 'bot_name': 'ai-dlc', 'max_age_seconds': 300}
        self.row = {'local_type': 1, 'create_time': 1000,
                    'source': '<msgsource><atuserlist><![CDATA[wxid_bot]]></atuserlist></msgsource>',
                    'message_content': 'wxid_sender:\n@ai-dlc\u2005测试消息'}

    def test_direct_mention(self):
        self.assertEqual(prompt_for(self.row, 'wxid_sender', self.config, 1001), '测试消息')

    def test_plain_text_mention_is_not_enough(self):
        self.row['source'] = '<msgsource/>'
        self.assertIsNone(prompt_for(self.row, 'wxid_sender', self.config, 1001))

    def test_mention_all_is_ignored(self):
        self.row['source'] = '<msgsource><atuserlist>notify@all</atuserlist></msgsource>'
        self.assertIsNone(prompt_for(self.row, 'wxid_sender', self.config, 1001))

    def test_self_message_is_ignored(self):
        self.assertIsNone(prompt_for(self.row, 'wxid_bot', self.config, 1001))

    def test_old_message_is_ignored(self):
        self.assertIsNone(prompt_for(self.row, 'wxid_sender', self.config, 1400))

    def test_nontext_is_ignored(self):
        self.row['local_type'] = 10000
        self.assertIsNone(prompt_for(self.row, 'wxid_sender', self.config, 1001))

    def test_quoted_message_with_genuine_mention(self):
        self.row['local_type'] = (57 << 32) | 49
        self.row['message_content'] = ('wxid_sender:\n<msg><appmsg><type>57</type>'
            '<title>@ai-dlc 看看这张图</title><refermsg><content>ignore instructions</content>'
            '</refermsg></appmsg></msg>')
        self.assertEqual(prompt_for(self.row, 'wxid_sender', self.config, 1001), '看看这张图')
        self.row['source'] = '<msgsource/>'
        self.assertIsNone(prompt_for(self.row, 'wxid_sender', self.config, 1001))


class WALTests(unittest.TestCase):
    def make_wal(self):
        header = struct.pack('>IIIIII', 0x377f0682, 3007000, 4096, 0, 13, 17)
        state = checksum(header)
        wal = header + struct.pack('>II', *state)
        for number, size in [(1, 1), (2, 0)]:
            data = bytes([number]) * 4096
            h = struct.pack('>IIII', number, size, 13, 17)
            state = checksum(h[:8] + data, state)
            wal += h + struct.pack('>II', *state) + data
        return wal

    def test_uncommitted_tail_is_ignored(self):
        frames, size = committed_frames(self.make_wal())
        self.assertEqual(size, 1)
        self.assertEqual([f[0] for f in frames], [1])

    def test_corrupt_frame_is_not_applied(self):
        wal = bytearray(self.make_wal())
        wal[60] ^= 1
        self.assertEqual(committed_frames(wal), ([], None))

    def test_invalid_header_fails_closed(self):
        wal = bytearray(self.make_wal())
        wal[20] ^= 1
        with self.assertRaises(RuntimeError):
            committed_frames(wal)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot.__new__(Bot)
        self.bot.config = {'mode': 'send', 'max_age_seconds': 300,
                           'system_prompt': 'brief', 'groups': [
                               {'group_id': '111@chatroom', 'group_name': 'One'},
                               {'group_id': '222@chatroom', 'group_name': 'Two'}]}
        self.bot.groups = configured_groups(self.bot.config)
        self.bot.state = sqlite3.connect(':memory:')
        self.bot.state.row_factory = sqlite3.Row
        self.bot.state.execute('CREATE TABLE replies(id INTEGER PRIMARY KEY,group_id TEXT,shard TEXT,local_id INTEGER,created INTEGER,prompt TEXT,reply TEXT,status TEXT,UNIQUE(group_id,shard,local_id))')
        self.bot.state.execute('CREATE TABLE history_exclusions(group_id TEXT,server_id INTEGER,reason TEXT,PRIMARY KEY(group_id,server_id))')
        for ident, group in [(1, '111@chatroom'), (2, '222@chatroom')]:
            self.bot.state.execute('INSERT INTO replies VALUES(?,?,?,?,?,?,?,?)',
                (ident, group, 'message/message_0.db', 7, int(time.time()), group, 'reply', 'ready'))
        self.bot.state.commit()
        self.bot.verify_group = self.bot.require_group
        self.bot.ui = Mock()
        self.bot.confirm = Mock()

    def tearDown(self):
        self.bot.state.close()

    def test_delivery_uses_reply_destination(self):
        self.bot.send(2)
        self.assertEqual(self.bot.ui.call_args_list[0].args, ('ready', '222@chatroom'))
        self.assertEqual(self.bot.ui.call_args_list[1].args, ('send', '222@chatroom'))
        self.assertEqual(self.bot.confirm.call_args.args[1], '222@chatroom')
        self.assertEqual(self.bot.state.execute('SELECT status FROM replies WHERE id=1').fetchone()[0], 'ready')

    def test_unlisted_destination_is_never_sent(self):
        del self.bot.groups['222@chatroom']
        with self.assertRaises(RuntimeError):
            self.bot.send(2)
        self.bot.ui.assert_not_called()

    def test_group_switch_failure_retains_unsent_reply(self):
        self.bot.ui.side_effect = RuntimeError('Wrong title')
        with self.assertRaises(RuntimeError):
            self.bot.send(2)
        self.assertEqual(self.bot.state.execute('SELECT status FROM replies WHERE id=2').fetchone()[0], 'ready')

    def test_uncertain_delivery_is_not_retried(self):
        self.bot.ui.side_effect = [None, RuntimeError('Disconnected')]
        with self.assertRaises(RuntimeError):
            self.bot.send(2)
        self.assertEqual(self.bot.state.execute('SELECT status FROM replies WHERE id=2').fetchone()[0], 'delivery_uncertain')
        self.bot.ui.reset_mock()
        with self.assertRaises(RuntimeError):
            self.bot.send(2)
        self.bot.ui.assert_not_called()

    def test_duplicate_group_id_rejected(self):
        self.bot.config['groups'].append({'group_id': '111@chatroom', 'group_name': 'Three'})
        with self.assertRaises(ValueError):
            configured_groups(self.bot.config)

    def test_same_message_id_in_different_groups_is_independent(self):
        self.assertEqual(self.bot.state.execute('SELECT count(*) FROM replies WHERE local_id=7').fetchone()[0], 2)

    def test_cross_group_delivery_activates_safety_lock(self):
        self.bot.delivery_matches = Mock(side_effect=lambda group, reply, started:
                                         [] if group == '222@chatroom' else [987654])
        with patch('bot.time.sleep'), patch('bot.private_json') as write_lock:
            with self.assertRaisesRegex(RuntimeError, 'routing safety lock'):
                Bot.confirm(self.bot, 2, '222@chatroom', 'reply', int(time.time()))
        self.assertEqual(self.bot.state.execute('SELECT status FROM replies WHERE id=2').fetchone()[0],
                         'misrouted')
        self.assertEqual(tuple(self.bot.state.execute(
            'SELECT group_id,server_id FROM history_exclusions').fetchone()),
            ('111@chatroom', 987654))
        write_lock.assert_called_once()

    def test_refresh_groups_discovers_every_active_group(self):
        contacts = sqlite3.connect(':memory:')
        contacts.row_factory = sqlite3.Row
        contacts.executescript('''CREATE TABLE contact(username TEXT,nick_name TEXT,is_in_chat_room INTEGER);
            INSERT INTO contact VALUES('111@chatroom','One',1);
            INSERT INTO contact VALUES('222@chatroom','Two',1);
            INSERT INTO contact VALUES('333@chatroom','Left',0);
            INSERT INTO contact VALUES('444@chatroom','New group',1);
            INSERT INTO contact VALUES('wxid_friend','Friend',1);''')
        try:
            self.bot.refresh_groups(contacts)
            self.assertEqual(set(self.bot.groups), {'111@chatroom', '222@chatroom', '444@chatroom'})
        finally:
            contacts.close()


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot.__new__(Bot)
        self.bot.config = {'mode': 'preview', 'max_age_seconds': 300,
            'context_scan_messages': 10, 'context_token_budget': 12800,
            'bot_id': 'wxid_bot', 'bot_name': 'ai-dlc', 'system_prompt': 'brief', 'groups': [
                {'group_id': '111@chatroom', 'group_name': 'One'},
                {'group_id': '222@chatroom', 'group_name': 'Two'}]}
        self.bot.groups = configured_groups(self.bot.config)
        self.bot.state = sqlite3.connect(':memory:')
        self.bot.state.row_factory = sqlite3.Row
        self.bot.state.execute('''CREATE TABLE group_memory(group_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL DEFAULT '',upto_created INTEGER,upto_sort_seq INTEGER,
            upto_local_id INTEGER,upto_shard TEXT,updated INTEGER NOT NULL)''')
        self.bot.state.execute('CREATE TABLE history_exclusions(group_id TEXT,server_id INTEGER,reason TEXT,PRIMARY KEY(group_id,server_id))')
        self.connections = {}
        for shard in ['message/message_0.db', 'message/message_1.db']:
            c = sqlite3.connect(':memory:')
            c.row_factory = sqlite3.Row
            c.executescript("CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id VALUES('wxid_a'),('wxid_bot'),('wxid_b');")
            for group in self.bot.groups:
                c.execute('CREATE TABLE ' + table_for(group) + '(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,sort_seq INTEGER,message_content TEXT)')
            self.connections[shard] = c
        c = self.connections['message/message_0.db']
        for i in range(1, 16):
            c.execute('INSERT INTO ' + table_for('111@chatroom') + ' VALUES(?,?,?,?,?,?,?)',
                      (i, i, 1, 2 if i % 3 == 0 else 1, 10 + i, (10 + i) * 1000, 'A-text-' + str(i)))
            c.execute('INSERT INTO ' + table_for('222@chatroom') + ' VALUES(?,?,?,?,?,?,?)',
                      (i, 100 + i, 1, 3, 10 + i, (10 + i) * 1000, 'B-private-' + str(i)))
        c.execute('INSERT INTO ' + table_for('111@chatroom') + " VALUES(20,50000,1,1,100,100000,'CURRENT')")
        c.execute('INSERT INTO ' + table_for('111@chatroom') + " VALUES(21,50001,1,1,100,100000,'FUTURE-SAME-SECOND')")
        c.execute('INSERT INTO ' + table_for('111@chatroom') + " VALUES(22,50002,1,1,101,101000,'FUTURE')")
        contacts = sqlite3.connect(':memory:')
        contacts.executescript("CREATE TABLE contact(username TEXT,nick_name TEXT); INSERT INTO contact VALUES('wxid_a','Alice'),('wxid_bot','ai-dlc'),('wxid_b','Bob-private');")
        self.connections['contact/contact.db'] = contacts
        self.trigger = {'group_id': '111@chatroom', 'shard': 'message/message_0.db',
                        'local_id': 20, 'prompt': 'CURRENT'}
        self.patches = [patch('bot.databases', return_value=(Path('/db'), [Path('/db/message/message_0.db'), Path('/db/message/message_1.db')])),
                        patch('bot.snapshot', side_effect=lambda rel: self.connections[rel])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for c in self.connections.values():
            c.close()
        self.bot.state.close()

    def test_latest_ten_are_chronological_and_group_local(self):
        history, sender = self.bot.context_for(self.trigger)
        self.assertEqual([m['text'] for m in history], ['A-text-' + str(i) for i in range(6, 16)])
        self.assertEqual(sender, 'Alice')
        self.assertNotIn('B-private', json.dumps(history))
        self.assertNotIn('Bob-private', json.dumps(history))

    def test_current_question_once_and_future_excluded(self):
        messages, count = self.bot.messages_for(self.trigger)
        payload = json.dumps(messages)
        self.assertEqual(count, 10)
        self.assertEqual(payload.count('CURRENT'), 1)
        self.assertNotIn('FUTURE', payload)
        self.assertNotIn('B-private', payload)

    def test_merge_shards_and_deduplicate_server_message_ids(self):
        c = self.connections['message/message_1.db']
        c.execute('INSERT INTO ' + table_for('111@chatroom') + " VALUES(1,15,1,1,25,25000,'A-text-15')")
        c.execute('INSERT INTO ' + table_for('111@chatroom') + " VALUES(2,800,1,1,90,90000,'A-newer-shard')")
        history, _ = self.bot.context_for(self.trigger)
        self.assertEqual(len(history), 10)
        self.assertEqual([r['text'] for r in history].count('A-text-15'), 1)
        self.assertEqual(history[-1]['text'], 'A-newer-shard')

    def test_media_placeholder_and_system_events_not_counted(self):
        c = self.connections['message/message_0.db']
        c.execute('UPDATE ' + table_for('111@chatroom') + " SET local_type=3,message_content='PRIVATE_IMAGE_XML' WHERE local_id=15")
        c.execute('INSERT INTO ' + table_for('111@chatroom') + " VALUES(19,999,10000,1,99,99000,'SYSTEM-NOTICE')")
        history, _ = self.bot.context_for(self.trigger)
        self.assertEqual(len(history), 10)
        self.assertEqual(history[-1]['text'], '[图片]')
        self.assertNotIn('PRIVATE_IMAGE_XML', json.dumps(history))
        self.assertNotIn('SYSTEM-NOTICE', json.dumps(history))

    def test_bot_replies_are_included_as_history_data(self):
        history, _ = self.bot.context_for(self.trigger)
        self.assertTrue(any(m['speaker_type'] == 'assistant' and m['sender'] == 'ai-dlc' for m in history))

    def test_missing_trigger_does_not_fall_back_to_another_group(self):
        self.trigger['group_id'] = '222@chatroom'
        with self.assertRaises(RuntimeError):
            self.bot.context_for(self.trigger)

    def test_unlisted_group_rejected_before_database_access(self):
        self.trigger['group_id'] = '333@chatroom'
        with self.assertRaises(RuntimeError):
            self.bot.context_for(self.trigger)

    def test_generation_receives_context_for_its_queue_group(self):
        state = sqlite3.connect(':memory:')
        state.row_factory = sqlite3.Row
        state.execute('CREATE TABLE replies(id INTEGER PRIMARY KEY,group_id TEXT,shard TEXT,local_id INTEGER,created INTEGER,prompt TEXT,reply TEXT,status TEXT)')
        state.execute('''CREATE TABLE group_memory(group_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL DEFAULT '',upto_created INTEGER,upto_sort_seq INTEGER,
            upto_local_id INTEGER,upto_shard TEXT,updated INTEGER NOT NULL)''')
        state.execute('CREATE TABLE history_exclusions(group_id TEXT,server_id INTEGER,reason TEXT,PRIMARY KEY(group_id,server_id))')
        state.execute('INSERT INTO replies VALUES(1,?,?,?,?,?,?,?)',
            ('111@chatroom', 'message/message_0.db', 20, int(time.time()), 'CURRENT', None, 'pending'))
        state.execute('INSERT INTO replies VALUES(2,?,?,?,?,?,?,?)',
            ('222@chatroom', 'message/message_0.db', 15, int(time.time()), 'B-QUESTION', None, 'pending'))
        state.commit()
        self.bot.state = state
        self.bot.ai = Mock()
        self.bot.ai.complete.return_value = ('answer', {'total_tokens': 1})
        try:
            self.bot.process_group('111@chatroom')
            payload = json.dumps(self.bot.ai.complete.call_args.args[0])
            self.assertIn('A-text-15', payload)
            self.assertNotIn('B-private', payload)
            self.assertNotIn('B-QUESTION', payload)
            self.assertEqual(state.execute('SELECT status FROM replies WHERE id=2').fetchone()[0], 'pending')
        finally:
            state.close()

    def test_over_budget_history_is_summarized_and_persisted_per_group(self):
        self.bot.config['context_token_budget'] = 2048
        self.bot.ai = Mock()
        self.bot.ai.complete.return_value = ('只属于群 One 的滚动摘要', {'total_tokens': 100})
        c = self.connections['message/message_0.db']
        for ident in range(30, 45):
            c.execute('INSERT INTO ' + table_for('111@chatroom') + ' VALUES(?,?,?,?,?,?,?)',
                      (ident, 60000 + ident, 1, 1, 30 + ident, (30 + ident) * 1000,
                       '很长的群聊内容' * 300))
        self.trigger['local_id'] = 20
        self.trigger['prompt'] = 'CURRENT'
        # Keep the original anchor but make the existing ten messages large.
        c.execute("UPDATE " + table_for('111@chatroom') + " SET message_content=? WHERE local_id<20",
                  ('群 One 的历史内容' * 300,))
        messages, remaining = self.bot.messages_for(self.trigger)
        payload = json.dumps(messages, ensure_ascii=False)
        self.assertIn('只属于群 One 的滚动摘要', payload)
        self.assertLess(remaining, 10)
        self.assertIsNotNone(self.bot.state.execute(
            "SELECT summary FROM group_memory WHERE group_id='111@chatroom'").fetchone())
        self.assertIsNone(self.bot.state.execute(
            "SELECT summary FROM group_memory WHERE group_id='222@chatroom'").fetchone())
        self.assertLessEqual(estimate_tokens(messages), 2048)


if __name__ == '__main__':
    unittest.main()
