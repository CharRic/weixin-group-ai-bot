import sqlite3
import unittest
from group_names import display_name
from bot import Bot


def member(user):
    data = b'\x0a' + bytes([len(user)]) + user.encode()
    return b'\x0a' + bytes([len(data)]) + data


class UnnamedGroups(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript('''CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,is_in_chat_room INTEGER);
            CREATE TABLE chat_room(username TEXT,ext_buffer BLOB);
            INSERT INTO contact VALUES('111@chatroom','Named','',1),('222@chatroom','','',1);
            INSERT INTO contact VALUES('bot','Bot','',0),('a','Alice','',0),('b','Bob','',0),('c','Carol','',0);''')
        self.c.execute('INSERT INTO chat_room VALUES(?,?)', ('222@chatroom', b''.join(member(s) for s in ['a','bot','b','c'])))
        self.bot = Bot.__new__(Bot)
        self.bot.config = {'bot_id': 'bot'}

    def tearDown(self):
        self.c.close()

    def test_unnamed_group_is_discovered_and_verified(self):
        self.bot.refresh_groups(self.c)
        self.assertEqual(self.bot.groups['222@chatroom']['group_name'], 'Alice、Bob、Carol')
        self.assertTrue(self.bot.groups['222@chatroom']['select_unique_result'])
        self.assertFalse(self.bot.groups['111@chatroom']['select_unique_result'])
        self.bot.verify_group('222@chatroom', self.c)

    def test_generated_title_collision_stops_delivery(self):
        self.c.execute("UPDATE contact SET nick_name='Alice、Bob、Carol' WHERE username='111@chatroom'")
        self.bot.refresh_groups(self.c)
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            self.bot.verify_group('222@chatroom', self.c)

    def test_incomplete_members_are_visible_but_not_routable(self):
        self.c.execute("DELETE FROM contact WHERE username='a'")
        self.bot.refresh_groups(self.c)
        self.assertIn('222@chatroom', self.bot.groups)
        with self.assertRaisesRegex(RuntimeError, 'not synced'):
            self.bot.verify_group('222@chatroom', self.c)

    def test_member_change_invalidates_previous_title(self):
        self.bot.refresh_groups(self.c)
        self.c.execute("UPDATE contact SET nick_name='New' WHERE username='a'")
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            self.bot.verify_group('222@chatroom', self.c)
