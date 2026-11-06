import tkinter as tk
import unittest


class TkSmokeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def test_instantiate_and_display(self):
        from hotel import tk_ui
        ui = tk_ui.TkinterUI(root=self.root)
        ui.info("hello")
        ui.success("ok")
        ui.error("err")
        ui.warning("warn")
        ui.box("title", "content")
        ui.show_menu("m", ["1. a", "2. b"], subtitle="sub")
        ui.show_table("t", ["h1", "h2"], [[1, 2]])
        ui.show_status_table("s", ["h"], [["Available"]], status_col=0)
        ui.clear_screen()

    def test_ask_text_returns_submitted_value(self):
        from hotel import tk_ui
        ui = tk_ui.TkinterUI(root=self.root)
        self.root.after(50, lambda: (ui.entry_var.set("typed"), ui._submit()))
        val = ui.ask_text("prompt:")
        self.assertEqual(val, "typed")

    def test_ask_number_validates(self):
        from hotel import tk_ui
        ui = tk_ui.TkinterUI(root=self.root)
        # first submit invalid, then valid
        def seq():
            ui.entry_var.set("abc")
            ui._submit()
            self.root.after(50, lambda: (ui.entry_var.set("5"), ui._submit()))
        self.root.after(50, seq)
        val = ui.ask_number("num:", minimum=1, maximum=10)
        self.assertEqual(val, 5)


if __name__ == "__main__":
    unittest.main()
