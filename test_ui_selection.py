"""Search regression tests; mocked X11 only, no messages or real UI actions."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from PIL import Image, ImageDraw
from bot_ui import ui


class SearchSelectionTests(unittest.TestCase):
    def select(self, unique, avatar_positions=(45,)):
        geometry = NS(x=96, y=87, width=320, height=240)
        image = Image.new('RGB', (320, 240), 'white')
        draw = ImageDraw.Draw(image)
        for y in avatar_positions:
            draw.rectangle((10, y, 42, y + 32), fill='gray')
        shot = NS(data=image.tobytes('raw', 'BGRX'))
        popup = Mock()
        popup.get_geometry.return_value = geometry
        popup.get_attributes.return_value = NS(map_state=2)
        root = Mock()
        root.translate_coords.return_value = NS(x=22, y=29)
        root.query_tree.return_value = NS(children=[popup])
        root.get_image.return_value = shot
        calls = []
        def xdo(*args):
            calls.append(args)
            if (args[0] == 'mousemove' and '--window' not in args) or args[-1] == 'Return':
                popup.get_attributes.return_value = NS(map_state=0)
        d = Mock()
        d.screen.return_value = NS(root=root)
        with patch.object(ui.display, 'Display', return_value=d), patch.object(ui, 'paste'), patch.object(ui, 'xdo', side_effect=xdo):
            ui.select_group(NS(id=7), 'Member A、Member B', unique)
        return calls

    def test_unnamed_opens_label_not_avatar(self):
        calls = self.select(True)
        self.assertIn(('mousemove', 220, 148, 'click', '--repeat', 2, '--delay', 120, '1'), calls)
        self.assertNotIn(('key', '--clearmodifiers', 'Return'), calls)

    def test_named_keeps_keyboard_selection(self):
        self.assertIn(('key', '--clearmodifiers', 'Return'), self.select(False))

    def test_ambiguous_unnamed_results_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            self.select(True, (45, 110))

    def test_no_avatar_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'could not be identified'):
            self.select(True, ())
