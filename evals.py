"""
evals.py — Eval tối thiểu: 3 nhiệm vụ nhỏ, mỗi nhiệm vụ chạy nhiều lần (mặc định 3).

Mỗi nhiệm vụ gồm:
  - setup(sandbox):  chuẩn bị file đầu vào trong một sandbox riêng, sạch
  - prompt:          nhiệm vụ giao cho agent
  - check(sandbox):  chấm ĐÚNG/SAI bằng code → (đúng?, lý do)

Model không tất định (mỗi lần chạy có thể khác nhau), nên phải chạy nhiều lần
mới thấy được tỷ lệ thành công thật.

Chạy:  python evals.py            # 3 lần mỗi nhiệm vụ
       python evals.py --runs 1   # chạy nhanh
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import agent
import setup_demo

RUNS_PER_TASK = 3
EVAL_DIR = agent.PROJECT_DIR / "eval_runs"  # mỗi lần chạy có sandbox riêng: eval_runs/<nhiệm vụ>_<lần>/


# ---------- Nhiệm vụ 1: sinh dữ liệu và ghi file ----------
PRIMES_PROMPT = "Tạo file primes.txt chứa 15 số nguyên tố đầu tiên, mỗi số một dòng, không có nội dung nào khác."


def check_primes(sandbox: Path) -> tuple[bool, str]:
    f = sandbox / "primes.txt"
    if not f.exists():
        return False, "không có primes.txt"
    got = [line.strip() for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected = ["2", "3", "5", "7", "11", "13", "17", "19", "23", "29", "31", "37", "41", "43", "47"]
    return (True, "đúng 15 số") if got == expected else (False, f"nội dung sai: {got[:20]}")


# ---------- Nhiệm vụ 2: phân tích dữ liệu ----------
SALES_PROMPT = (
    "File sales.csv chứa dữ liệu bán hàng. Với mỗi tháng, tìm sản phẩm có tổng doanh thu "
    "(quantity × unit_price) cao nhất. Ghi kết quả ra top_products.json: một object JSON ánh xạ "
    'tháng (dạng "YYYY-MM") sang tên sản phẩm, ví dụ {"YYYY-MM": "tên sản phẩm"}.'
)


def setup_sales(sandbox: Path) -> None:
    setup_demo.write_sales_csv(sandbox / "sales.csv")


def check_sales(sandbox: Path) -> tuple[bool, str]:
    f = sandbox / "top_products.json"
    if not f.exists():
        return False, "không có top_products.json"
    try:
        got = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return False, f"JSON không hợp lệ: {e}"
    expected = {month: product for month, (product, _) in setup_demo.top_product_by_month(setup_demo.sales_rows()).items()}
    return (True, "đúng cả 3 tháng") if got == expected else (False, f"nhận {got}, đúng phải là {expected}")


# ---------- Nhiệm vụ 3: sửa lỗi code ----------
BUGGY_STATS = '''\
def median(numbers):
    """Trả về trung vị của một danh sách số."""
    n = len(numbers)
    return numbers[n // 2]
'''
MEDIAN_PROMPT = (
    "Hàm median(numbers) trong stats_utils.py đang trả về kết quả sai. Hãy sửa để hàm trả về đúng "
    "trung vị của mọi danh sách số không rỗng (kể cả danh sách chưa sắp xếp hoặc có số phần tử chẵn). "
    "Giữ nguyên tên file và tên hàm."
)
MEDIAN_CASES = [  # (đầu vào, trung vị đúng)
    ([3, 1, 2], 2),
    ([4, 1, 3, 2], 2.5),
    ([7], 7),
    ([10, 2, 38, 23, 38, 23], 23),
    ([-5, 0, -1, 10], -0.5),
]


def setup_median(sandbox: Path) -> None:
    (sandbox / "stats_utils.py").write_text(BUGGY_STATS, encoding="utf-8")


def check_median(sandbox: Path) -> tuple[bool, str]:
    # Chạy code của agent trong tiến trình riêng (có timeout) để chấm, không import thẳng vào đây.
    inputs = [numbers for numbers, _ in MEDIAN_CASES]
    code = f"import json, stats_utils; print(json.dumps([stats_utils.median(list(c)) for c in {inputs!r}]))"
    try:
        proc = subprocess.run([sys.executable, "-c", code], cwd=sandbox, capture_output=True, text=True,
                              timeout=10, env={"PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return False, "median chạy quá 10 giây"
    if proc.returncode != 0:
        return False, f"lỗi khi chạy: {proc.stderr.strip()[-200:]}"
    try:
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        wrong = [(numbers, g, e) for (numbers, e), g in zip(MEDIAN_CASES, got) if abs(g - e) > 1e-9]
    except (ValueError, IndexError, TypeError) as e:
        return False, f"không đọc được kết quả: {e}"
    return (True, f"đúng {len(MEDIAN_CASES)} ca kiểm thử") if not wrong else (False, f"sai ở {wrong}")


TASKS = [
    {"name": "primes", "prompt": PRIMES_PROMPT, "setup": lambda sandbox: None, "check": check_primes},
    {"name": "sales_top", "prompt": SALES_PROMPT, "setup": setup_sales, "check": check_sales},
    {"name": "fix_median", "prompt": MEDIAN_PROMPT, "setup": setup_median, "check": check_median},
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Chạy eval cho agent.")
    parser.add_argument("--runs", type=int, default=RUNS_PER_TASK, help="số lần chạy mỗi nhiệm vụ")
    args = parser.parse_args()

    client = agent.make_client()
    results = []  # mỗi phần tử: (tên nhiệm vụ, đúng?, số bước, số token)
    print(f"Model: {agent.MODEL} · {len(TASKS)} nhiệm vụ × {args.runs} lần\n")
    for task in TASKS:
        for i in range(1, args.runs + 1):
            sandbox = EVAL_DIR / f"{task['name']}_{i}"
            shutil.rmtree(sandbox, ignore_errors=True)  # sandbox sạch cho mỗi lần chạy
            sandbox.mkdir(parents=True)
            task["setup"](sandbox)
            print(f"  {task['name']:<11} lần {i}: ", end="", flush=True)
            run = agent.run_agent(task["prompt"], sandbox=sandbox, client=client,
                                  verbose=False, label=f"eval-{task['name']}-{i}")
            ok, reason = task["check"](sandbox)
            results.append((task["name"], ok, run.steps, run.tokens))
            print(f"{'ĐÚNG' if ok else 'SAI '} · {run.steps:>2} bước · {run.tokens:>7,} token · {run.status} · {reason}")

    # Bảng tổng kết
    groups = {t["name"]: [r for r in results if r[0] == t["name"]] for t in TASKS}
    groups["TỔNG"] = results
    print(f"\n{'Nhiệm vụ':<12}{'Thành công':>14}{'Bước TB':>10}{'Token TB':>12}")
    for name, rows in groups.items():
        passed = sum(r[1] for r in rows)
        rate = f"{passed}/{len(rows)} ({passed / len(rows):.0%})"
        avg_steps = sum(r[2] for r in rows) / len(rows)
        avg_tokens = sum(r[3] for r in rows) / len(rows)
        print(f"{name:<12}{rate:>14}{avg_steps:>10.1f}{avg_tokens:>12,.0f}")


if __name__ == "__main__":
    main()
