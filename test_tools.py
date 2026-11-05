"""
test_tools.py — Kiểm thử tools.py mà không gọi API (không cần API key).

Chạy:  python -m unittest test_tools -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools

PROJECT_DIR = Path(__file__).resolve().parent


class ToolsTest(unittest.TestCase):
    def setUp(self):
        # Sandbox tạm nằm NGAY TRONG thư mục project và bị xoá sau mỗi test.
        self.tmp = tempfile.TemporaryDirectory(dir=PROJECT_DIR, prefix=".test_sandbox_")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, **args):
        return tools.execute_tool(name, args, self.root)

    def test_write_then_read(self):
        out, err = self.call("write_file", path="sub/a.txt", content="xin chào")
        self.assertFalse(err, out)
        self.assertEqual(self.call("read_file", path="sub/a.txt"), ("xin chào", False))

    def test_blocks_paths_outside_sandbox(self):
        for bad in ["/etc/passwd", "../agent.py", "sub/../../agent.py", "~/.bashrc"]:
            out, err = self.call("read_file", path=bad)
            self.assertTrue(err, f"'{bad}' lẽ ra phải bị chặn")
            self.assertIn("bị chặn", out)

    def test_blocks_symlink_escape(self):
        os.symlink(PROJECT_DIR, self.root / "escape")  # symlink trong sandbox trỏ ra ngoài
        out, err = self.call("read_file", path="escape/agent.py")
        self.assertTrue(err)
        self.assertIn("ngoài sandbox", out)
        out, err = self.call("write_file", path="escape/hacked.txt", content="x")
        self.assertTrue(err)
        self.assertFalse((PROJECT_DIR / "hacked.txt").exists())

    def test_read_file_truncates(self):
        (self.root / "big.txt").write_text("x" * (tools.MAX_READ_CHARS + 100))
        out, err = self.call("read_file", path="big.txt")
        self.assertFalse(err)
        self.assertIn("ĐÃ CẮT", out)

    def test_list_files(self):
        self.call("write_file", path="a.txt", content="1")
        self.call("write_file", path="d/b.txt", content="22")
        out, err = self.call("list_files")
        self.assertFalse(err)
        self.assertIn("a.txt (1 bytes)", out)
        self.assertIn("d/b.txt (2 bytes)", out)

    def test_run_python_runs_inside_sandbox(self):
        self.call("write_file", path="n.txt", content="41")
        out, err = self.call("run_python", code="print(int(open('n.txt').read()) + 1)")
        self.assertFalse(err, out)
        self.assertIn("42", out)

    def test_run_python_error_has_traceback(self):
        out, err = self.call("run_python", code="1 / 0")
        self.assertTrue(err)
        self.assertIn("ZeroDivisionError", out)

    def test_run_python_hides_api_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-secret"}):
            out, err = self.call("run_python", code="import os; print(os.environ.get('ANTHROPIC_API_KEY'))")
        self.assertFalse(err, out)
        self.assertNotIn("test-secret", out)

    def test_run_python_timeout(self):
        with mock.patch.object(tools, "RUN_PYTHON_TIMEOUT_S", 1):
            out, err = self.call("run_python", code="while True: pass")
        self.assertTrue(err)
        self.assertIn("quá 1 giây", out)

    def test_bad_tool_name_and_args(self):
        out, err = self.call("delete_everything")
        self.assertTrue(err)
        self.assertIn("list_files", out)  # lời nhắn liệt kê các tool hợp lệ
        out, err = self.call("read_file", file="a.txt")
        self.assertTrue(err)
        self.assertIn("path", out)  # lời nhắn cho biết tên tham số đúng


if __name__ == "__main__":
    unittest.main()
