# type: ignore
"""Unit tests for the ui.py menu renderer.

The behaviour pinned here: `show_menu(..., subtitle=...)` renders the
"Signed in as ..." line above the entries, and that line is escaped so a
username or email containing `[` cannot be read as rich markup and either
break the render or inject a style.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotel import ui


class ShowMenuSubtitleTests(unittest.TestCase):
    def _rendered(self, **kwargs):
        """Render one menu and return its panel, with the console silenced."""
        with mock.patch.object(ui.console, "print") as render:
            ui.show_menu("Test Menu", ["1. First", "2. Second"], **kwargs)
        render.assert_called_once()
        return render.call_args[0][0]

    def test_subtitle_is_rendered_above_the_entries(self):
        panel = self._rendered(subtitle="Signed in as Ada (ada@example.com)")
        body = str(panel.renderable)
        lines = body.splitlines()
        # Dim line first, a blank spacer, then the entries -- the session is read
        # before anyone picks an option, which is the whole point of it.
        self.assertIn("[dim]Signed in as Ada (ada@example.com)[/dim]", lines[0])
        self.assertEqual(lines[1], "")
        self.assertIn("[bold]1.[/bold] First", body)

    def test_menu_without_a_subtitle_is_unchanged(self):
        body = str(self._rendered().renderable)
        self.assertNotIn("[dim]", body)
        self.assertNotIn("Signed in as", body)
        self.assertIn("[bold]2.[/bold] Second", body)

    def test_title_and_positional_arguments_still_work(self):
        # test_onboarding.py asserts show_menu's positional args, so the subtitle
        # must stay a keyword-only addition to that contract.
        with mock.patch.object(ui.console, "print") as render:
            ui.show_menu("Admin Panel", ["1. Thing"])
        panel = render.call_args[0][0]
        self.assertEqual(panel.title, "Admin Panel")

    def test_markup_characters_in_the_subtitle_are_escaped(self):
        # A literal '[bold]' typed as a username must be shown, not applied.
        panel = self._rendered(subtitle="Signed in as [bold]x")
        body = str(panel.renderable)
        self.assertIn("\\[bold]x", body)
        self.assertNotIn("[bold]x[/bold]", body)
