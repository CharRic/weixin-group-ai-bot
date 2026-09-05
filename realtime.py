"""Read-only clock and weather capability; no arbitrary URLs or shared chat state."""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


def clock_context():
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    return ('当前真实北京时间（Asia/Shanghai，UTC+8）：' + now.strftime('%Y-%m-%d %H:%M:%S') +
            '，星期' + '一二三四五六日'[now.weekday()] + '。默认时间问题按北京时间回答并注明；'
            '今天、明天必须按此日期计算，不使用历史聊天中的旧时间。')


WEATHER_TOOL = {'type': 'function', 'function': {
    'name': 'get_weather',
    'description': '查询明确城市的当前天气及未来7天预报。只用当前提问或当前群中提问者明确指定的位置；'
                   '没有城市先问清楚，不能猜测用户位置。不可用于历史天气。',
    'parameters': {'type': 'object', 'properties': {
        'city': {'type': 'string', 'description': '城市名，不含天气问题，例如北京或Shanghai'},
        'country_code': {'type': 'string', 'description': '明确国家的两字母代码；不确定可省略'}},
        'required': ['city'], 'additionalProperties': False}}}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Weather:
    def __init__(self):
        self.cache = {}
        self.opener = urllib.request.build_opener(NoRedirect())

    def fetch(self, endpoint, params):
        # Endpoint is chosen exclusively by our code, never by the model.
        url = endpoint + '?' + urllib.parse.urlencode(params)
        with self.opener.open(url, timeout=8) as response:
            data = response.read(65537)
        if len(data) > 65536:
            raise ValueError('Weather response too large')
        return json.loads(data)

    def query(self, city, country_code=''):
        if not isinstance(city, str) or not re.fullmatch(r"[\w\u4e00-\u9fff .'-]{1,60}", city.strip()):
            return {'error': '请提供城市名，不要提供网址或地址。'}
        if not isinstance(country_code, str) or (country_code and not re.fullmatch('[A-Z]{2}', country_code)):
            return {'error': '国家代码无效。'}
        key = (city.strip(), country_code)
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        params = {'name': city.strip(), 'count': 5, 'language': 'zh', 'format': 'json'}
        if country_code:
            params['countryCode'] = country_code
        try:
            rows = self.fetch('https://geocoding-api.open-meteo.com/v1/search', params).get('results', [])
            if country_code:
                rows = [r for r in rows if r.get('country_code') == country_code]
            # Ignore small neighbourhood matches when the API found a city.
            cities = [r for r in rows if r.get('population', 0) >= 100000]
            rows = cities or rows
            if not rows:
                return {'error': '没有找到该城市，请补充城市或国家。'}
            if len(rows) != 1:
                return {'error': '城市有多个匹配，请先确认省份或国家。',
                        'choices': [{k: r.get(k) for k in ('name', 'admin1', 'country')} for r in rows]}
            place = rows[0]
            result = self.fetch('https://api.open-meteo.com/v1/forecast', {
                'latitude': place['latitude'], 'longitude': place['longitude'],
                'timezone': place['timezone'], 'forecast_days': 7,
                'current': 'temperature_2m,apparent_temperature,weather_code,wind_speed_10m',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max'})
            if not result.get('daily', {}).get('time'):
                raise ValueError('Missing forecast')
            output = {'source': 'Open-Meteo', 'source_url': 'https://open-meteo.com/',
                      'retrieved_at': datetime.now(ZoneInfo('UTC')).isoformat(),
                      'city': place['name'], 'region': place.get('admin1'), 'country': place.get('country'),
                      'timezone': result['timezone'], 'current': result.get('current'),
                      'current_units': result.get('current_units'), 'daily': result['daily'],
                      'daily_units': result.get('daily_units'),
                      'code_legend': 'WMO: 0晴;1主要晴;2局部多云;3阴;45/48雾;51/53/55毛毛雨;'
                          '56/57冻毛毛雨;61/63/65小/中/大雨;66/67冻雨;71/73/75小/中/大雪;'
                          '77米雪;80/81/82阵雨;85/86阵雪;95雷暴;96/99雷暴伴冰雹。'}
            if len(self.cache) >= 64:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), output)
            return output
        except (OSError, ValueError, KeyError, TypeError):
            return {'error': '天气接口暂时不可用，这次未查到实时数据。不能编造天气。'}


def estimate_tokens(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    cjk = len(re.findall(r'[\u3400-\u9fff\uf900-\ufaff]', text))
    return int((cjk + (len(text) - cjk + 3) // 4) * 1.25) + 16


def chat_complete(ai, messages, weather, token_budget=12800, extra_tools=(), tool_handler=None):
    """Bounded tools; only the caller's bound handler may perform sticker delivery."""
    inputs = list(messages)
    total = 0
    remaining = 2
    other_remaining = 4
    tools = ([WEATHER_TOOL] if weather is not None else []) + list(extra_tools)
    allowed_extra = {t['function']['name'] for t in extra_tools}
    for _ in range(4 if extra_tools else 2):
        if estimate_tokens({'messages': inputs, 'tools': tools}) > token_budget:
            return '这次工具资料太长了，请把问题缩小一点。', {'total_tokens': total}
        payload = {'model': ai.config['model'], 'messages': inputs, 'stream': False,
                   'max_tokens': ai.config.get('max_tokens', 1024), 'tools': tools,
                   'thinking': {'type': 'disabled'}}
        result = ai.request('/chat/completions', payload)
        total += result.get('usage', {}).get('total_tokens', 0)
        message = result['choices'][0]['message']
        calls = message.get('tool_calls') or []
        if not calls:
            text = message.get('content')
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError('API returned no text reply')
            return text, {'total_tokens': total}
        if len(calls) > 2:
            raise RuntimeError('Too many tool calls')
        inputs.append({'role': 'assistant', 'content': message.get('content'), 'tool_calls': calls})
        for call in calls:
            output = {'error': '无效工具参数，未执行查询。'}
            try:
                function = call['function']
                if remaining and weather is not None and function['name'] == 'get_weather' and len(function['arguments']) <= 1024:
                    args = json.loads(function['arguments'])
                    if isinstance(args, dict) and set(args) <= {'city', 'country_code'}:
                        remaining -= 1
                        output = weather.query(**args)
                elif other_remaining and function['name'] in allowed_extra and tool_handler and len(function['arguments']) <= 1024:
                    args = json.loads(function['arguments'])
                    if isinstance(args, dict):
                        other_remaining -= 1
                        output = tool_handler(function['name'], args)
            except (TypeError, ValueError, KeyError):
                pass
            inputs.append({'role': 'tool', 'tool_call_id': call['id'],
                           'content': json.dumps(output, ensure_ascii=False)})
    inputs.append({'role': 'system', 'content': '本轮查询次数已用完，仅依据已获得的数据简短回答。无数据时说明，不猜测。'})
    if estimate_tokens(inputs) > token_budget:
        return '这次工具资料太长了，请把问题缩小一点。', {'total_tokens': total}
    text, usage = ai.complete(inputs)
    return text, {'total_tokens': total + usage.get('total_tokens', 0)}
