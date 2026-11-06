import unittest

try:
    import tkinter as tk
    from tkinter import simpledialog
    _root = tk.Tk()
    _root.withdraw()
    _root.destroy()
    _TK_AVAILABLE = True
except Exception:
    _TK_AVAILABLE = False


@unittest.skipUnless(_TK_AVAILABLE, "Tkinter display not available")
class TestTkUI(unittest.TestCase):
    def setUp(self):
        from hotel import tk_ui
        self.ui = tk_ui.TkUI()
        self.ui.root.withdraw()

    def tearDown(self):
        try:
            self.ui.root.destroy()
        except Exception:
            pass

    def test_instantiate_hidden_root(self):
        self.assertIsNotNone(self.ui.root)
        self.ui.info("hello")
        self.ui.success("ok")
        self.ui.warning("w")

    def test_ask_text_queued_answer(self):
        from tkinter import simpledialog
        simpledialog.askstring = lambda *a, **k: "queued answer"
        result = self.ui.ask_text("prompt", default="d")
        self.assertEqual(result, "queued answer")

    def test_ask_confirmation_queued(self):
        from tkinter import messagebox
        messagebox.askyesno = lambda *a, **k: True
        self.assertTrue(self.ui.ask_confirmation("sure?"))

    def test_show_menu_and_table(self):
        self.ui.show_menu("Title", ["1. A", "2. B"], subtitle="sub")
        self.ui.show_table("T", ["h1", "h2"], [["a", "b"]])
        self.ui.show_status_table("S", ["h"], [["x"]])
        self.ui.box("B", "content")
        self.ui.clear_screen()

    def test_busy_toggle(self):
        self.ui.set_busy(True, "Working")
        self.ui.set_busy(False)


if __name__ == "__main__":
    unittest.main()
