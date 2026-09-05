"""Constrained native WeChat sticker search/favorites; never type a chat message."""
import base64
import hashlib
import io
import json
import re
import subprocess
import sys
import time
from PIL import Image
from Xlib import display
import ui


def panel(d, w):
    root = d.screen().root
    origin = root.translate_coords(w.id, 0, 0)
    matches = []
    for child in root.query_tree().children:
        g = child.get_geometry()
        if (child.get_attributes().map_state == 2 and 450 <= g.width <= 490 and 460 <= g.height <= 500
                and abs(g.x - origin.x - 68) <= 4 and abs(g.y - origin.y - 93) <= 4):
            matches.append(g)
    if len(matches) != 1:
        raise RuntimeError('Sticker panel is not uniquely visible')
    return matches[0]


def picture(d, g):
    data = d.screen().root.get_image(g.x, g.y, g.width, g.height, 2, 0xffffffff)
    return Image.frombytes('RGB', (g.width, g.height), data.data, 'raw', 'BGRX')


def header(image, source):
    # Exclude the blinking search caret; query text is independently copied/checked.
    return hashlib.sha256(image.crop((0, 65 if source == 'search' else 0,
                                    image.width, 100 if source == 'search' else 35)).tobytes()).hexdigest()


def verify_panel(d, w, template, g):
    if ui.signature(w) != template:
        raise RuntimeError('Group title changed')
    root = d.screen().root
    prop = root.get_full_property(d.intern_atom('_NET_ACTIVE_WINDOW'), 0)
    if not prop:
        raise RuntimeError('No active window')
    active = d.create_resource_object('window', int(prop.value[0]))
    origin = root.translate_coords(active.id, 0, 0)
    size = active.get_geometry()
    if ('wechat' not in str(active.get_wm_class()).lower() or
            abs(origin.x - g.x) > 2 or abs(origin.y - g.y) > 2 or
            size.width != g.width or size.height != g.height):
        raise RuntimeError('Native sticker panel is not active')


def set_query(g, query):
    keeper = subprocess.Popen(['xclip', '-selection', 'clipboard', '-in', '-quiet'],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        keeper.stdin.write(query.encode())
        keeper.stdin.close()
        time.sleep(.1)
        ui.xdo('mousemove', g.x + 120, g.y + 38, 'click', '1')
        ui.xdo('key', '--clearmodifiers', 'ctrl+a', 'ctrl+v')
        time.sleep(.5)
        ui.xdo('mousemove', g.x + 120, g.y + 38, 'click', '1')
        ui.xdo('key', '--clearmodifiers', 'ctrl+a', 'ctrl+c')
        time.sleep(.1)
        copied = subprocess.check_output(['xclip', '-selection', 'clipboard', '-o'], timeout=3).decode()
        if copied != query:
            raise RuntimeError('Native search text verification failed')
        # Arrow/Enter are tab navigation shortcuts in this popup, not editor keys.
    finally:
        if keeper.poll() is None:
            keeper.terminate()
            keeper.wait(timeout=3)


def main():
    payload = json.load(sys.stdin)
    group = payload['group_id']
    if not re.fullmatch('[0-9]+@chatroom', group):
        raise ValueError('Explicit group required')
    source = payload.get('source', 'search')
    if source not in ('search', 'favorites'):
        raise ValueError('Invalid sticker source')
    profiles = json.loads((ui.ROOT / 'titles.json').read_text())
    template = profiles[group]['signature']
    d = display.Display(':1')
    w = ui.main_window(d)
    if sys.argv[1] == 'prepare':
        ui.verify(d, w, template)
        if w.get_geometry().width != 980 or w.get_geometry().height != 710:
            raise RuntimeError('Unsupported native sticker layout')
        query = payload.get('query', '')
        if not isinstance(query, str) or not 1 <= len(query) <= 30 or any(ord(c) < 32 for c in query):
            raise ValueError('Invalid sticker query')
        ui.xdo('key', 'Escape')
        ui.xdo('mousemove', '--window', w.id, 300, 586, 'click', '1')
        time.sleep(.3)
        g = panel(d, w)
        verify_panel(d, w, template, g)
        ui.xdo('mousemove', g.x + (35 if source == 'search' else 133), g.y + g.height - 24, 'click', '1')
        time.sleep(.3)
        if source == 'search':
            set_query(g, query)
            # Search is live. Enter changes tabs in this client, so do not press it.
            time.sleep(2)
            ui.xdo('mousemove', '--window', w.id, 850, 42)
        else:
            time.sleep(.5)
        g = panel(d, w)
        im = picture(d, g)
        out = io.BytesIO()
        im.save(out, format='PNG')
        print(json.dumps({'image': base64.b64encode(out.getvalue()).decode(),
            'header': header(im, source), 'geometry': [g.x, g.y, g.width, g.height]}))
    elif sys.argv[1] == 'send':
        if time.time() - payload['prepared'] > 90:
            raise RuntimeError('Sticker selection expired')
        g = panel(d, w)
        if [g.x, g.y, g.width, g.height] != payload['geometry']:
            raise RuntimeError('Sticker panel moved')
        if header(picture(d, g), source) != payload['header']:
            raise RuntimeError('Sticker query or panel changed')
        verify_panel(d, w, template, g)
        if source == 'search':
            ui.xdo('mousemove', g.x + 120, g.y + 38, 'click', '1')
            ui.xdo('key', '--clearmodifiers', 'ctrl+a', 'ctrl+c')
            time.sleep(.1)
            copied = subprocess.check_output(['xclip', '-selection', 'clipboard', '-o'], timeout=3).decode()
            if copied != payload['query']:
                raise RuntimeError('Search keyword changed')
        x, y = payload['point']
        if not (20 <= x <= g.width - 20 and (115 if source == 'search' else 40) <= y <= g.height - 65):
            raise ValueError('Selection is outside sticker content')
        verify_panel(d, w, template, g)
        ui.xdo('mousemove', g.x + x, g.y + y, 'click', '1')
        time.sleep(.3)
        ui.xdo('key', 'Escape')
        print(json.dumps({'submitted': True}))
    else:
        raise ValueError('Unknown native sticker action')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
