import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from social_memory import SocialMemory, command, digest, member_key


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SocialMemory(self.temp.name, {'social_memory_batch_messages': 6})
        self.now = int(time.time())
        self.group = '111@chatroom'
        self.room = digest(self.group)
        self.member = member_key(self.group, 'alice')

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, text='我喜欢摄影', sender='alice', source='1', group=None):
        self.store.observe(group or self.group, sender, source, int(time.time()), text)

    def extract(self, messages, **kwargs):
        data = json.loads(messages[1]['content'])
        event = data['events'][0]
        item = {'slot': 'interest', 'text': event['text'], 'kind': 'self_report',
                'evidence': [{'id': event['id'], 'quote': event['text'][:100]}]}
        return json.dumps({'members': [{'member': event['member'], 'items': [item]}]}), {}

    def ai(self):
        return Mock(complete=Mock(side_effect=self.extract))

    def test_disk_restart_and_same_person_group_isolation(self):
        self.observe()
        self.store.update_once(self.ai(), [self.group])
        other = SocialMemory(self.temp.name)
        self.assertIn('摄影', other.context(self.group, 'alice'))
        self.assertNotIn('摄影', other.context('222@chatroom', 'alice'))
        self.assertNotIn('摄影', other.context(self.group, 'bob'))
        self.assertTrue(self.store.path(self.room, self.member).exists())
        self.assertFalse(any(isinstance(v, list) for v in vars(other).values()))

    def test_no_historical_backfill_or_sensitive_copy(self):
        self.store.observe(self.group, 'alice', 'old', self.now - 100, '我喜欢旧内容')
        self.observe('我的手机号是13800138000')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 0)

    def test_event_dedup_and_monotonic_ids(self):
        self.observe()
        self.observe()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
            first = db.execute('SELECT id FROM events').fetchone()[0]
        self.store.update_once(self.ai(), [self.group])
        self.observe(source='2')
        with self.store.db() as db:
            self.assertGreater(db.execute('SELECT id FROM events').fetchone()[0], first)

    def test_threshold_and_timer_and_active_group_gate(self):
        self.observe('今天天空很蓝')
        ai = self.ai()
        self.assertFalse(self.store.update_once(ai, [self.group]))
        ai.complete.assert_not_called()
        with patch('social_memory.time.time', return_value=self.now + 21601):
            self.assertFalse(self.store.update_once(ai, ['222@chatroom']))
            self.assertTrue(self.store.update_once(ai, [self.group]))

    def test_batch_count_trigger(self):
        for i in range(6):
            self.observe('今晚一起聊天', source=str(i))
        self.assertTrue(self.store.update_once(self.ai(), [self.group]))

    def test_bad_extraction_backoff_and_preserves_queue(self):
        self.observe()
        ai = Mock(complete=Mock(return_value=('not json', {})))
        with self.assertRaises(ValueError):
            self.store.update_once(ai, [self.group])
        self.assertFalse(self.store.update_once(ai, [self.group]))
        self.assertEqual(ai.complete.call_count, 1)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)

    def test_forget_stops_collection_and_resume_is_local(self):
        self.observe()
        self.store.update_once(self.ai(), [self.group])
        self.store.control(self.group, 'alice', '忘记我')
        self.assertFalse(self.store.path(self.room, self.member).exists())
        self.observe(source='2')
        self.assertFalse(self.store.update_once(self.ai(), [self.group]))
        self.assertNotIn('摄影', self.store.context(self.group, 'alice'))
        self.assertIn('停止', self.store.control(self.group, 'alice', '查看我的记忆'))
        self.store.control('222@chatroom', 'alice', '恢复记忆')
        self.observe(source='3')
        self.assertFalse(self.store.update_once(self.ai(), [self.group]))
        self.store.control(self.group, 'alice', '恢复记忆')
        self.observe(source='4')
        self.assertTrue(self.store.update_once(self.ai(), [self.group]))

    def test_forget_cancels_inflight_update(self):
        self.observe()
        def callback(messages, **kwargs):
            result = self.extract(messages)
            self.store.control(self.group, 'alice', '忘记我')
            return result
        self.assertFalse(self.store.update_once(Mock(complete=callback), [self.group]))
        self.assertFalse(self.store.path(self.room, self.member).exists())

    def test_resume_does_not_ingest_delayed_optout_period_messages(self):
        self.store.control(self.group, 'alice', '忘记我')
        with patch('social_memory.time.time', return_value=self.now + 100):
            self.store.control(self.group, 'alice', '恢复记忆')
            self.store.observe(self.group, 'alice', 'delayed', self.now + 10, '我喜欢画画')
            self.assertFalse(self.store.update_once(self.ai(), [self.group]))

    def test_correction_replaces_slot(self):
        self.observe()
        self.store.update_once(self.ai(), [self.group])
        self.observe('我更喜欢画画', source='2')
        self.store.update_once(self.ai(), [self.group])
        self.assertIn('画画', self.store.context(self.group, 'alice'))
        self.assertNotIn('摄影', self.store.context(self.group, 'alice'))

    def test_expired_profile_not_loaded(self):
        self.observe()
        self.store.update_once(self.ai(), [self.group])
        with patch('social_memory.time.time', return_value=self.now + 181 * 86400):
            self.assertNotIn('摄影', self.store.context(self.group, 'alice'))

    def test_evidence_must_be_exact_and_same_speaker(self):
        events = {1: {'member': 'a', 'text': '我喜欢摄影'}}
        item = {'slot': 'interest', 'text': '喜欢摄影', 'kind': 'self_report',
                'evidence': [{'id': 1, 'quote': '喜欢摄影'}]}
        self.assertTrue(self.store.valid_item(item, 'a', events))
        self.assertFalse(self.store.valid_item(item, 'b', events))
        item['evidence'][0]['quote'] = '喜欢医疗'
        self.assertFalse(self.store.valid_item(item, 'a', events))

    def test_observation_requires_repeated_evidence(self):
        events = {i: {'member': 'a', 'text': '回复短一点'} for i in range(1, 4)}
        item = {'slot': 'communication', 'text': '偏好简短回复', 'kind': 'observation',
                'evidence': [{'id': 1, 'quote': '短一点'}]}
        self.assertFalse(self.store.valid_item(item, 'a', events))
        item['evidence'] = [{'id': i, 'quote': '短一点'} for i in events]
        self.assertTrue(self.store.valid_item(item, 'a', events))
        item['slot'] = 'background'
        self.assertFalse(self.store.valid_item(item, 'a', events))

    def test_no_arbitrary_paths(self):
        with self.assertRaises(ValueError):
            self.store.path('../../elsewhere', self.member)
        self.assertNotEqual(member_key('1', 'same'), member_key('2', 'same'))

    def test_style_requires_two_independent_multi_member_batches(self):
        def style_ai(messages, **kwargs):
            events = json.loads(messages[1]['content'])['events']
            return json.dumps({'members': [], 'style': {'tone': 'warm', 'humor': 'light',
                'detail': 'brief', 'pace': 'lively', 'evidence': [e['id'] for e in events]}}), {}
        for round_number in range(2):
            for i in range(6):
                self.observe('今晚一起聊天', sender=str(i % 3), source=f'{round_number}:{i}')
            self.store.update_once(Mock(complete=style_ai), [self.group])
            self.assertEqual(bool(self.store.read(self.room).get('style')), round_number == 1)
        self.assertIn('温和亲近', self.store.context(self.group, 'alice'))
        self.assertNotIn('温和亲近', self.store.context('222@chatroom', 'alice'))

    def test_prompt_and_reply_context_are_bounded(self):
        for i in range(40):
            self.observe('我喜欢' + '很长的聊天内容' * 140, sender=str(i % 8), source=str(i))
        ai = self.ai()
        self.store.update_once(ai, [self.group])
        self.assertLessEqual(len(ai.complete.call_args.args[0][1]['content']), 6500)
        self.assertLess(len(self.store.context(self.group, 'alice')), 1000)

    def test_exact_control_only(self):
        self.assertEqual(command('忘记我！'), 'forget')
        self.assertIsNone(command('小王说忘记我'))
        self.assertIsNone(command('忘记小王'))

    def test_related_profiles_local_bounded_and_optout_respected(self):
        for i in range(5):
            self.observe('我喜欢摄影' + str(i), sender='p' + str(i), source=str(i))
            self.store.update_once(self.ai(), [self.group])
        related = [{'sender': 'p' + str(i), 'name': '同名'} for i in range(5)]
        value = json.loads(self.store.context(self.group, 'alice', related))
        self.assertEqual(len(value['related_members']), 3)
        self.assertEqual(len({r['speaker_key'] for r in value['related_members']}), 3)
        self.assertNotIn('摄影', self.store.context('222@chatroom', 'alice', related))
        self.store.control(self.group, 'p0', '忘记我')
        value = json.loads(self.store.context(self.group, 'alice', related))
        self.assertNotIn(member_key(self.group, 'p0'), json.dumps(value))

    def test_document_hard_limits_and_rolling_slots(self):
        self.observe()
        self.store.update_once(self.ai(), [self.group])
        for i in range(10):
            self.observe('我喜欢摄影' + str(i), source=str(i + 2))
            self.store.update_once(self.ai(), [self.group])
        data = self.store.read(self.room, self.member)
        self.assertEqual(len(data['items']), 1)
        self.assertLessEqual(self.store.path(self.room, self.member).stat().st_size, 16000)
        with self.assertRaises(ValueError):
            self.store.write(self.room, {'style': 'x' * 5000})


if __name__ == '__main__':
    unittest.main()
