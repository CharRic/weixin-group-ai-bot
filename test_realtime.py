import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from realtime import Weather, chat_complete, clock_context


class ClockTests(unittest.TestCase):
    def test_clock_is_shanghai_not_server_default_timezone(self):
        with patch('realtime.datetime') as clock:
            clock.now.return_value = datetime(2026, 9, 5, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai'))
            result = clock_context()
        self.assertIn('2026-09-05 23:59', result)
        self.assertIn('星期六', result)
        self.assertIn('UTC+8', result)
        self.assertEqual(str(clock.now.call_args.args[0]), 'Asia/Shanghai')


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.weather = Weather()
        self.place = {'name': '北京', 'latitude': 39.9, 'longitude': 116.4,
                      'country_code': 'CN', 'country': '中国', 'timezone': 'Asia/Shanghai', 'population': 1000000}
        self.forecast = {'timezone': 'Asia/Shanghai', 'daily': {'time': ['2026-09-05', '2026-09-06'],
            'temperature_2m_max': [25, 26], 'temperature_2m_min': [15, 16], 'weather_code': [1, 3]},
            'daily_units': {'temperature_2m_max': '°C'}}
        self.weather.fetch = Mock(side_effect=[{'results': [self.place]}, self.forecast])

    def test_forecast_is_dated_sourced_and_cached(self):
        result = self.weather.query('北京', 'CN')
        self.assertEqual(result['daily']['time'][1], '2026-09-06')
        self.assertEqual(result['source'], 'Open-Meteo')
        self.assertEqual(self.weather.query('北京', 'CN'), result)
        self.assertEqual(self.weather.fetch.call_count, 2)

    def test_arbitrary_url_never_fetched(self):
        self.assertIn('error', self.weather.query('https://evil.example/'))
        self.weather.fetch.assert_not_called()

    def test_missing_city_and_bad_country_fail_closed(self):
        for args in [('', ''), ('北京', 'China'), (None, '')]:
            self.assertIn('error', self.weather.query(*args))
        self.weather.fetch.assert_not_called()

    def test_ambiguous_places_require_clarification(self):
        self.weather.fetch.side_effect = [{'results': [self.place, dict(self.place, country='其他')]}]
        self.assertIn('choices', self.weather.query('北京'))
        self.assertEqual(self.weather.fetch.call_count, 1)

    def test_network_failure_does_not_invent_forecast(self):
        self.weather.fetch.side_effect = TimeoutError()
        result = self.weather.query('北京')
        self.assertIn('error', result)
        self.assertNotIn('daily', result)

    def test_country_filter_is_enforced(self):
        self.assertIn('error', self.weather.query('北京', 'US'))
        self.assertEqual(self.weather.fetch.call_count, 1)


def response(content=None, calls=None):
    return {'choices': [{'message': {'content': content, 'tool_calls': calls}}], 'usage': {'total_tokens': 10}}


def call(name='get_weather', arguments='{"city":"北京"}', ident='c1'):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.ai = Mock(config={'model': 'deepseek-v4-flash'})
        self.weather = Mock()
        self.weather.query.return_value = {'city': '北京', 'temperature': 25}
        self.messages = [{'role': 'system', 'content': 'current group only'}, {'role': 'user', 'content': '北京明天天气'}]

    def test_ordinary_chat_uses_one_request(self):
        self.ai.request.return_value = response('北京时间13:00。')
        text, _ = chat_complete(self.ai, self.messages, self.weather)
        self.assertIn('北京时间', text)
        self.weather.query.assert_not_called()
        self.assertEqual(self.ai.request.call_count, 1)

    def test_weather_result_is_sent_to_flash(self):
        self.ai.request.side_effect = [response(calls=[call()]), response('北京明天25度。')]
        text, usage = chat_complete(self.ai, self.messages, self.weather)
        self.weather.query.assert_called_once_with(city='北京')
        payload = self.ai.request.call_args.args[1]
        self.assertEqual(payload['model'], 'deepseek-v4-flash')
        self.assertEqual(json.loads(payload['messages'][-1]['content'])['temperature'], 25)
        self.assertEqual(usage['total_tokens'], 20)
        self.assertEqual(len(self.messages), 2)  # No shared conversation mutation.

    def test_unrecognized_tool_cannot_execute(self):
        self.ai.request.side_effect = [response(calls=[call('shell')]), response('不能执行。')]
        chat_complete(self.ai, self.messages, self.weather)
        self.weather.query.assert_not_called()

    def test_invalid_arguments_are_rejected(self):
        self.ai.request.side_effect = [response(calls=[call(arguments='{"url":"https://evil.example"}')]), response('城市呢？')]
        chat_complete(self.ai, self.messages, self.weather)
        self.weather.query.assert_not_called()

    def test_two_query_limit_and_no_unbounded_tool_loop(self):
        self.ai.request.side_effect = [response(calls=[call(), call(ident='c2')]), response(calls=[call(ident='c3')])]
        self.ai.complete.return_value = ('仅回答已知结果。', {'total_tokens': 5})
        _, usage = chat_complete(self.ai, self.messages, self.weather)
        self.assertEqual(self.weather.query.call_count, 2)
        self.ai.complete.assert_called_once()
        self.assertEqual(usage['total_tokens'], 25)

    def test_tool_context_respects_configured_budget(self):
        chat_complete(self.ai, self.messages, self.weather, token_budget=10)
        self.ai.request.assert_not_called()
        self.weather.query.assert_not_called()

    def test_confirmed_sticker_only_stops_without_another_model_call(self):
        from native_stickers import STICKER_TOOLS
        self.ai.request.return_value = response(calls=[call('send_sticker', '{"sticker_id":"chosen"}')])
        handler = Mock(return_value={'status':'confirmed','finish_without_text':True})
        text, _ = chat_complete(self.ai,self.messages,self.weather,extra_tools=STICKER_TOOLS,tool_handler=handler)
        self.assertEqual(text, '')
        self.ai.request.assert_called_once()

    def test_uncertain_sticker_does_not_silently_finish(self):
        from native_stickers import STICKER_TOOLS
        self.ai.request.side_effect = [response(calls=[call('send_sticker', '{"sticker_id":"chosen"}')]),response('这次没确认发成功。')]
        handler = Mock(return_value={'status':'delivery_uncertain','finish_without_text':True})
        text, _ = chat_complete(self.ai,self.messages,self.weather,extra_tools=STICKER_TOOLS,tool_handler=handler)
        self.assertTrue(text)
        self.assertEqual(self.ai.request.call_count,2)
