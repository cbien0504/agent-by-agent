"""
tools.py — 4 công cụ (tool) của agent: list_files, read_file, write_file, run_python.

Mỗi tool gồm hai phần:
  1. Một hàm Python làm việc thật (bên dưới).
  2. Một JSON schema mô tả tool cho model (TOOL_SCHEMAS). Model chỉ "nhìn thấy" phần này,
     nên tên, mô tả và tham số phải rõ ràng.

An toàn:
  - Mọi đường dẫn đều đi qua safe_path() và bị ép nằm trong thư mục sandbox.
  - Lỗi mà agent tự sửa được sẽ được ném ra dưới dạng ToolError, kèm lời nhắn "sai gì, nên thử gì".
    agent.py gửi lời nhắn đó lại cho model với is_error=True thay vì làm sập chương trình.
"""

import os
import subprocess
import sys
from pathlib import Path

# ============ Giới hạn của tool (chỉnh ở đây) ============
RUN_PYTHON_TIMEOUT_S = 10  # run_python bị dừng nếu chạy lâu hơn số giây này
MAX_READ_CHARS = 20_000    # read_file trả về tối đa chừng này ký tự
MAX_OUTPUT_CHARS = 5_000   # stdout và stderr của run_python, mỗi thứ tối đa chừng này ký tự
MAX_LIST_ENTRIES = 200     # list_files liệt kê tối đa chừng này mục

# Tool chỉ đọc, không thay đổi gì trong sandbox → chạy song song với nhau an toàn.
READ_ONLY_TOOLS = {"list_files", "read_file"}


class ToolError(Exception):
    """Lỗi mà agent có thể tự sửa. Lời nhắn được gửi cho model kèm is_error=True."""


def truncate(text: str, limit: int) -> str:
    """Cắt chuỗi quá dài và ghi rõ là đã cắt, để model biết nó không thấy hết."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... đã cắt bớt {len(text) - limit:,} ký tự]"


# --------------------------------------------------------------------------
# Hàng rào sandbox: MỌI đường dẫn do model đưa vào đều phải đi qua hàm này.
# --------------------------------------------------------------------------
def safe_path(root: Path, path: str) -> Path:
    """Đổi đường dẫn tương đối của model thành đường dẫn thật, bảo đảm nằm trong root."""
    raw = (path or ".").strip() or "."
    if Path(raw).is_absolute() or raw.startswith("~"):
        raise ToolError(
            f"Đường dẫn '{path}' bị chặn: chỉ dùng đường dẫn tương đối tính từ gốc sandbox, "
            "ví dụ 'data.csv' hoặc 'reports/summary.md'."
        )
    if ".." in Path(raw).parts:
        raise ToolError(
            f"Đường dẫn '{path}' bị chặn vì có '..' (không được đi ra ngoài sandbox). "
            "Hãy dùng đường dẫn tương đối tính từ gốc sandbox."
        )
    root = root.resolve()
    full = (root / raw).resolve()  # resolve() đi theo symlink → lộ ra vị trí thật
    if not full.is_relative_to(root):
        raise ToolError(
            f"Đường dẫn '{path}' bị chặn vì thực chất trỏ ra ngoài sandbox (qua symlink). "
            "Chỉ làm việc với file nằm trong sandbox."
        )
    return full


# --------------------------------------------------------------------------
# 4 tool. Tham số đầu tiên luôn là root (thư mục sandbox); phần còn lại do model cung cấp.
# --------------------------------------------------------------------------
def list_files(root: Path, path: str = ".") -> str:
    target = safe_path(root, path)
    if not target.exists():
        raise ToolError(f"Không tìm thấy '{path}'. Gọi list_files với path='.' để xem toàn bộ sandbox.")
    if target.is_file():
        return f"{path} ({target.stat().st_size:,} bytes)"

    entries = []  # (đường dẫn, có phải thư mục không)
    for dirpath, dirnames, filenames in os.walk(target):  # os.walk không đi theo symlink thư mục
        entries += [(Path(dirpath) / d, True) for d in dirnames]
        entries += [(Path(dirpath) / f, False) for f in filenames]
    if not entries:
        return "(thư mục trống)"

    lines = []
    for p, is_dir in sorted(entries)[:MAX_LIST_ENTRIES]:
        rel = p.relative_to(target).as_posix()
        lines.append(f"{rel}/" if is_dir else f"{rel} ({p.lstat().st_size:,} bytes)")
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"[... còn {len(entries) - MAX_LIST_ENTRIES} mục nữa không hiển thị]")
    return "\n".join(lines)


def read_file(root: Path, path: str) -> str:
    target = safe_path(root, path)
    if not target.exists():
        raise ToolError(f"Không tìm thấy file '{path}'. Dùng list_files để xem các file đang có.")
    if target.is_dir():
        raise ToolError(f"'{path}' là thư mục, không phải file. Dùng list_files(path='{path}') để xem bên trong.")
    text = target.read_text(encoding="utf-8", errors="replace")
    if not text:
        return "(file rỗng)"
    if len(text) > MAX_READ_CHARS:
        return (
            text[:MAX_READ_CHARS]
            + f"\n\n[ĐÃ CẮT: chỉ hiện {MAX_READ_CHARS:,}/{len(text):,} ký tự đầu. "
            "Nếu cần phần còn lại, dùng run_python để đọc hoặc xử lý file.]"
        )
    return text


def write_file(root: Path, path: str, content: str) -> str:
    target = safe_path(root, path)
    if target.is_dir():
        raise ToolError(f"'{path}' là thư mục. Hãy ghi vào một file, ví dụ '{path}/output.txt'.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Đã ghi {len(content):,} ký tự vào '{path}'."


def run_python(root: Path, code: str) -> str:
    # Biến môi trường tối giản: KHÔNG chuyển ANTHROPIC_API_KEY (hay biến nào khác của bạn) vào tiến trình con.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),               # '~' trong code của agent cũng trỏ vào sandbox
        "PYTHONUTF8": "1",               # open() và print() mặc định dùng UTF-8
        "PYTHONDONTWRITEBYTECODE": "1",  # không sinh __pycache__ trong sandbox
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,  # code gọi input() sẽ nhận EOF ngay thay vì treo
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_PYTHON_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"Code chạy quá {RUN_PYTHON_TIMEOUT_S} giây nên đã bị dừng. "
            "Kiểm tra vòng lặp vô hạn, lệnh input(), hoặc giảm lượng tính toán rồi thử lại."
        ) from None
    result = (
        f"exit code: {proc.returncode}\n"
        f"--- stdout ---\n{truncate(proc.stdout, MAX_OUTPUT_CHARS)}\n"
        f"--- stderr ---\n{truncate(proc.stderr, MAX_OUTPUT_CHARS)}"
    )
    if proc.returncode != 0:
        raise ToolError(result + "\nCode bị lỗi: đọc traceback ở stderr, sửa code rồi chạy lại.")
    return result


# --------------------------------------------------------------------------
# Mô tả tool cho model (JSON schema). Đây là thứ được gửi lên API trong tham số `tools`.
# --------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": (
            "Liệt kê đệ quy các file (kèm kích thước) và thư mục trong sandbox. "
            "Nên gọi đầu tiên để biết sandbox đang có gì."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Thư mục cần liệt kê, tương đối với gốc sandbox. Mặc định '.' (gốc).",
                }
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Đọc nội dung một file văn bản trong sandbox. "
            f"Nếu file dài hơn {MAX_READ_CHARS:,} ký tự thì chỉ trả về phần đầu, kèm ghi chú là đã cắt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Đường dẫn tương đối của file, ví dụ 'sales.csv'."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Ghi một file văn bản (UTF-8) trong sandbox: tạo mới, hoặc ghi đè toàn bộ nếu file đã có. "
            "Tự tạo thư mục cha nếu chưa có."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Đường dẫn tương đối của file, ví dụ 'report.md'."},
                "content": {"type": "string", "description": "Toàn bộ nội dung cần ghi vào file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Chạy một đoạn code Python 3 trong tiến trình riêng; thư mục làm việc là gốc sandbox "
            "(nên open('sales.csv') đọc được file trong sandbox). Chỉ có thư viện chuẩn, "
            f"không có pandas hay numpy. Timeout {RUN_PYTHON_TIMEOUT_S} giây. Chỉ trả về exit code, "
            "stdout và stderr (đã cắt ngắn), nên hãy print() những gì bạn cần xem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Mã Python hoàn chỉnh cần chạy."}},
            "required": ["code"],
        },
    },
]

TOOL_FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_python": run_python,
}


def execute_tool(name: str, args: dict, root: Path) -> tuple[str, bool]:
    """Chạy một tool theo tên. Trả về (output, is_error). Không bao giờ để exception lọt ra ngoài."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Không có tool tên '{name}'. Các tool hợp lệ: {', '.join(TOOL_FUNCTIONS)}.", True

    # Kiểm tra tham số theo JSON schema (bản đơn giản): đủ tham số bắt buộc, không có tham số lạ.
    schema = next(s["input_schema"] for s in TOOL_SCHEMAS if s["name"] == name)
    allowed = list(schema["properties"])
    missing = [k for k in schema["required"] if k not in args]
    unknown = [k for k in args if k not in allowed]
    if missing or unknown:
        return (
            f"Tham số không hợp lệ cho '{name}': thiếu {missing}, thừa {unknown}. "
            f"Tool này nhận các tham số: {allowed}."
        ), True

    try:
        return func(root, **args), False
    except ToolError as e:
        return str(e), True
    except Exception as e:  # lỗi ngoài dự kiến: vẫn báo cho model thay vì làm sập agent
        return f"Lỗi không mong đợi khi chạy '{name}': {type(e).__name__}: {e}", True
