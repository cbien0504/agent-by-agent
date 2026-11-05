"""
agent.py — Agent dòng lệnh tối giản dùng Anthropic API với tool use gốc.

Toàn bộ "trí thông minh" nằm ở model; phần code này chỉ là một VÒNG LẶP (xem run_agent):

    messages = [nhiệm vụ]
    lặp lại:
        phản hồi = gọi model(system prompt, tools, messages)
        nếu model muốn dùng tool → chạy tool, nối kết quả vào messages, lặp tiếp
        nếu model đã xong        → in câu trả lời, dừng

Cách chạy:
    python agent.py "Tạo file hello.txt chứa lời chào"
    python agent.py --file demo_task.md
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic

import tools

# ============ Cấu hình và giới hạn an toàn (chỉnh ở đây) ============
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
MAX_STEPS = 15           # số lần gọi model tối đa cho một nhiệm vụ
TOKEN_BUDGET = 200_000   # tổng token tối đa cho một nhiệm vụ (vào + ra, kể cả phần cache)
TOOL_TIMEOUT_S = 30      # thời gian tối đa cho mỗi lần chạy tool (run_python có thêm timeout 10s riêng)
MAX_TOKENS = 16_000      # độ dài tối đa của MỘT phản hồi từ model (tính cả phần thinking)
EFFORT = "medium"        # model nghĩ kỹ đến mức nào: low | medium | high | xhigh | max

PROJECT_DIR = Path(__file__).resolve().parent
SANDBOX_DIR = PROJECT_DIR / "sandbox"
TRACES_DIR = PROJECT_DIR / "traces"
TRACE_MAX_CHARS = 2_000  # output của tool được cắt còn chừng này ký tự khi ghi vào trace

# ============ System prompt: vai trò, quy tắc, định dạng câu trả lời ============
# Giữ cố định (không chèn ngày giờ...) để prompt caching hoạt động.
SYSTEM_PROMPT = f"""\
Bạn là một agent tự động làm việc với file và dữ liệu bên trong một thư mục sandbox.
Bạn có 4 công cụ: list_files, read_file, write_file, run_python. Mọi đường dẫn đều tương đối
với gốc sandbox, và run_python cũng chạy với thư mục làm việc là gốc sandbox.

Quy tắc:
1. Trước khi bắt đầu, nói ngắn gọn trong một câu bạn định làm gì. Sau đó làm từng bước nhỏ:
   xem dữ liệu thật trước khi xử lý; mỗi lần gọi công cụ chỉ làm một việc rõ ràng.
2. Không đoán số liệu: mọi con số phải được tính bằng run_python từ dữ liệu thật.
3. Kiểm tra trước khi báo xong: đọc lại file đã ghi hoặc chạy code kiểm tra độc lập.
   Nếu thấy sai thì sửa rồi kiểm tra lại.
4. Khi công cụ báo lỗi: đọc kỹ lời nhắn, sửa nguyên nhân rồi thử lại; không lặp lại y nguyên
   lệnh vừa lỗi.
5. Nếu yêu cầu mơ hồ đến mức các cách hiểu khác nhau cho kết quả khác nhau, hãy dừng và hỏi lại
   người dùng thay vì đoán. Nếu chỉ thiếu chi tiết nhỏ, tự chọn cách hợp lý và nêu giả định.
6. Chỉ làm việc trong sandbox: không đọc, ghi hay chạy thứ gì bên ngoài, kể cả qua run_python.
   Python chỉ có thư viện chuẩn (không có pandas, numpy).
7. Mỗi nhiệm vụ có tối đa {MAX_STEPS} lượt gọi model, nên đừng lãng phí lượt. Các lần đọc độc lập
   với nhau có thể gọi cùng lúc trong một lượt.

Khi xong, trả lời ngắn gọn bằng ngôn ngữ của người dùng, theo mẫu:
Kết quả: 1–3 câu trả lời thẳng vào nhiệm vụ.
File: các file đã tạo hoặc sửa (nếu có).
Đã kiểm tra: bạn đã kiểm tra kết quả bằng cách nào.
"""

# Danh sách tools gửi lên API: gắn cache_control vào tool CUỐI để cache toàn bộ danh sách tools.
CACHED_TOOLS = tools.TOOL_SCHEMAS[:-1] + [
    {**tools.TOOL_SCHEMAS[-1], "cache_control": {"type": "ephemeral"}}
]

# Giải thích ngắn cho từng kiểu kết thúc khác "done"
STOP_NOTES = {
    "max_steps": f"Dừng vì đã dùng hết {MAX_STEPS} bước (MAX_STEPS).",
    "token_budget": f"Dừng vì đã vượt ngân sách {TOKEN_BUDGET:,} token (TOKEN_BUDGET).",
    "max_tokens": f"Phản hồi bị cắt vì chạm MAX_TOKENS = {MAX_TOKENS:,}. Tăng giá trị này hoặc chia nhỏ nhiệm vụ.",
    "refusal": "Model từ chối thực hiện yêu cầu này.",
    "api_error": "Lỗi khi gọi API:",
}


@dataclass
class RunResult:
    status: str       # "done" nếu xong bình thường; còn lại là lý do dừng (xem STOP_NOTES)
    answer: str       # câu trả lời cuối của model ("" nếu không có)
    steps: int        # số lần đã gọi model
    tokens: int       # tổng token đã dùng
    trace_path: Path


# ---------------------------------------------------------------------------
# Trace: mỗi sự kiện là một dòng JSON trong traces/<timestamp>.jsonl
# ---------------------------------------------------------------------------
class Trace:
    def __init__(self, label: str = ""):
        TRACES_DIR.mkdir(exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + (f"_{label}" if label else "")
        self.path = TRACES_DIR / f"{name}.jsonl"
        n = 2
        while self.path.exists():  # chạy 2 lần trong cùng một giây thì thêm hậu tố
            self.path = TRACES_DIR / f"{name}-{n}.jsonl"
            n += 1

    def log(self, **record) -> None:
        record = {"time": datetime.now().isoformat(timespec="seconds"), **record}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# Gọi model
# ---------------------------------------------------------------------------
def make_client() -> anthropic.Anthropic:
    """Tạo client. API key chỉ đọc từ biến môi trường: không hardcode, không in ra."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "Lỗi: chưa đặt biến môi trường ANTHROPIC_API_KEY.\n"
            "Đặt key rồi chạy lại, ví dụ:  export ANTHROPIC_API_KEY='sk-ant-...'"
        )
    return anthropic.Anthropic()  # SDK tự đọc key từ ANTHROPIC_API_KEY


def call_model(client: anthropic.Anthropic, messages: list):
    """Một lượt gọi Messages API: gửi system prompt + tools + toàn bộ hội thoại."""
    return client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # Prompt caching. Prefix được ghép theo thứ tự tools → system → messages:
        #   - tool cuối có cache_control (CACHED_TOOLS) → cache danh sách tools
        #   - system có cache_control                  → cache tools + system prompt
        #   - cache_control ở cấp request              → tự cache phần hội thoại đang dài ra
        tools=CACHED_TOOLS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        cache_control={"type": "ephemeral"},
        messages=messages,
        # Model nghĩ trước khi trả lời (adaptive thinking). "summarized" trả về bản tóm tắt
        # suy nghĩ để in ra màn hình; mặc định phần này bị ẩn.
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": EFFORT},
    )


def usage_of(response) -> dict:
    """Token của một lượt gọi. Kích thước prompt = input + cache_write + cache_read."""
    u = response.usage
    return {
        "input": u.input_tokens,                            # phần không nằm trong cache (giá gốc)
        "cache_write": u.cache_creation_input_tokens or 0,  # ghi mới vào cache (đắt hơn giá gốc 25%)
        "cache_read": u.cache_read_input_tokens or 0,       # đọc lại từ cache (rẻ hơn 10 lần)
        "output": u.output_tokens,                          # model sinh ra, gồm cả phần thinking
    }


def describe_api_error(e: anthropic.APIError) -> str:
    """Đổi lỗi API thành lời giải thích dễ hiểu (không bao giờ chứa API key)."""
    if isinstance(e, anthropic.AuthenticationError):
        return "API key không hợp lệ hoặc đã bị thu hồi (401). Kiểm tra ANTHROPIC_API_KEY."
    if isinstance(e, anthropic.NotFoundError):
        return f"Không tìm thấy model '{MODEL}' (404). Kiểm tra biến môi trường MODEL."
    if isinstance(e, anthropic.RateLimitError):
        return "Vượt giới hạn tốc độ (429) dù SDK đã tự thử lại. Đợi một lúc rồi chạy lại."
    if isinstance(e, anthropic.APIStatusError):
        return f"API trả lỗi {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Không kết nối được tới API (mạng hoặc timeout)."
    return f"Lỗi API: {e}"


# ---------------------------------------------------------------------------
# Chạy tool
# ---------------------------------------------------------------------------
_pool = ThreadPoolExecutor(max_workers=4)


def run_tool_calls(calls: list, sandbox: Path) -> list[tuple[str, bool, float]]:
    """Chạy các lệnh gọi tool của một lượt. Trả về [(output, is_error, số giây)] theo đúng thứ tự.

    - Nếu mọi lệnh đều chỉ đọc (list_files, read_file) → chạy song song.
    - Nếu có lệnh ghi file hoặc chạy code → chạy lần lượt, đúng thứ tự model yêu cầu,
      để không đọc một file trước khi nó được ghi xong.
    """

    def timed(call):
        t0 = time.monotonic()
        output, is_error = tools.execute_tool(call.name, call.input, sandbox)
        return output, is_error, time.monotonic() - t0

    def wait(future):
        try:
            return future.result(timeout=TOOL_TIMEOUT_S)
        except FutureTimeout:  # tool treo: báo lỗi cho model thay vì chờ mãi
            return f"Tool chạy quá {TOOL_TIMEOUT_S} giây nên bị bỏ qua. Hãy thử cách nhẹ hơn.", True, TOOL_TIMEOUT_S

    if all(call.name in tools.READ_ONLY_TOOLS for call in calls):
        futures = [_pool.submit(timed, call) for call in calls]  # bắt đầu tất cả cùng lúc
        return [wait(f) for f in futures]
    return [wait(_pool.submit(timed, call)) for call in calls]  # từng cái một


# ---------------------------------------------------------------------------
# Vòng lặp agent
# ---------------------------------------------------------------------------
def run_agent(task: str, *, messages: list | None = None, sandbox: Path = SANDBOX_DIR,
              client: anthropic.Anthropic | None = None, verbose: bool = True, label: str = "") -> RunResult:
    """Chạy agent cho tới khi model trả lời xong hoặc chạm một giới hạn an toàn.

    messages là CONTEXT của agent: toàn bộ hội thoại (nhiệm vụ, phản hồi, kết quả tool).
    Truyền cùng một list qua nhiều lần gọi thì agent nhớ những gì đã làm (dùng khi hỏi tiếp).
    """
    client = client or make_client()
    sandbox.mkdir(parents=True, exist_ok=True)
    if messages is None:
        messages = []
    messages.append({"role": "user", "content": task})

    show = print if verbose else (lambda *args, **kwargs: None)
    trace = Trace(label)
    trace.log(event="start", task=task, model=MODEL, sandbox=str(sandbox),
              limits={"max_steps": MAX_STEPS, "token_budget": TOKEN_BUDGET, "tool_timeout_s": TOOL_TIMEOUT_S})
    show(f"▶ Nhiệm vụ: {short(task, 200)}")
    show(f"  model={MODEL} · sandbox={sandbox} · trace={trace.path.relative_to(PROJECT_DIR)}")

    status, answer, step, used = "max_steps", "", 0, 0
    started = time.monotonic()
    while step < MAX_STEPS:
        if used >= TOKEN_BUDGET:
            status = "token_budget"
            break
        step += 1

        # 1) Gọi model với toàn bộ context hiện có
        show(f"\n[Bước {step}] đang gọi model...", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            response = call_model(client, messages)
        except anthropic.APIError as e:
            status, answer = "api_error", describe_api_error(e)
            show("lỗi")
            trace.log(event="api_error", step=step, error=answer)
            break
        model_s = time.monotonic() - t0
        usage = usage_of(response)
        used += sum(usage.values())
        show(f"{model_s:.1f}s · token vào {usage['input']:,} · ghi cache {usage['cache_write']:,} · "
             f"đọc cache {usage['cache_read']:,} · ra {usage['output']:,} (tổng {used:,})")
        thoughts = [b.thinking for b in response.content if b.type == "thinking" and b.thinking]
        texts = [b.text for b in response.content if b.type == "text" and b.text.strip()]
        for t in thoughts:
            show(f"  [nghĩ] {short(t, 300)}")
        record = {"event": "step", "step": step, "stop_reason": response.stop_reason,
                  "model_seconds": round(model_s, 2), "usage": usage, "thinking": thoughts, "text": texts}

        # 2) Model muốn dùng tool → chạy tool, nối kết quả vào context, lặp tiếp
        if response.stop_reason == "tool_use":
            for t in texts:
                show(f"  [nói] {short(t, 300)}")
            messages.append({"role": "assistant", "content": response.content})  # giữ nguyên mọi khối
            calls = [b for b in response.content if b.type == "tool_use"]
            results = run_tool_calls(calls, sandbox)
            messages.append({"role": "user", "content": [  # TẤT CẢ kết quả gửi trong MỘT message
                {"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": is_error}
                for call, (output, is_error, _) in zip(calls, results)
            ]})
            record["tool_calls"] = []
            for call, (output, is_error, seconds) in zip(calls, results):
                show(f"  [tool] {call.name}({fmt_args(call.input)})")
                show(f"         → {'LỖI' if is_error else 'OK'} sau {seconds:.2f}s: {short(output, 160)}")
                record["tool_calls"].append({
                    "name": call.name, "input": call.input, "is_error": is_error,
                    "seconds": round(seconds, 3), "output": tools.truncate(output, TRACE_MAX_CHARS),
                })
            trace.log(**record)
            continue

        # 3) Model dừng: xong (end_turn), hoặc dừng bất thường (max_tokens, refusal, ...)
        trace.log(**record)
        answer = "\n".join(texts).strip()
        if response.stop_reason == "end_turn":
            status = "done"
            messages.append({"role": "assistant", "content": response.content})
        else:
            # Không nối phản hồi dở dang vào context, để hội thoại vẫn hợp lệ nếu hỏi tiếp.
            status = response.stop_reason
            details = getattr(response, "stop_details", None)
            if status == "refusal" and details is not None:
                answer = (answer + f"\n(Loại từ chối: {details.category})").strip()
        break

    seconds = time.monotonic() - started
    trace.log(event="end", status=status, steps=step, total_tokens=used, seconds=round(seconds, 1), answer=answer)
    show(f"\n══ Kết thúc: {status} · {step} bước · {used:,} token · {seconds:.1f}s ══")
    if status != "done":
        show(STOP_NOTES.get(status, f"Model dừng với stop_reason = {status}."))
    if answer:
        show(answer)
    return RunResult(status, answer, step, used, trace.path)


# ---------------------------------------------------------------------------
# Tiện ích in ra màn hình
# ---------------------------------------------------------------------------
def short(text, limit: int) -> str:
    """Gộp về một dòng và rút gọn, để in ra màn hình."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_args(args: dict) -> str:
    """In gọn tham số của lệnh gọi tool, ví dụ: path='sales.csv'"""
    return ", ".join(f"{k}={short(repr(v), 70)}" for k, v in args.items())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Agent CLI demo dùng Anthropic API (tool use).")
    parser.add_argument("task", nargs="?", help="nhiệm vụ bằng ngôn ngữ tự nhiên")
    parser.add_argument("-f", "--file", help="đọc nhiệm vụ từ file, ví dụ demo_task.md")
    args = parser.parse_args()

    if args.file:
        if not Path(args.file).is_file():
            parser.error(f"không tìm thấy file {args.file}")
        task = Path(args.file).read_text(encoding="utf-8")
    else:
        task = args.task or input("Nhập nhiệm vụ: ")
    if not task.strip():
        parser.error("chưa có nhiệm vụ")

    client = make_client()
    messages = []  # context dùng chung cho cả các câu hỏi tiếp theo
    run_agent(task, messages=messages, client=client)

    # Ở terminal tương tác: cho phép trả lời câu hỏi của agent hoặc giao việc tiếp, giữ nguyên context.
    while sys.stdin.isatty():
        try:
            follow_up = input("\nTrả lời agent hoặc giao việc tiếp (Enter để thoát): ").strip()
        except EOFError:
            break
        if not follow_up:
            break
        run_agent(follow_up, messages=messages, client=client)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nĐã dừng (Ctrl+C).")
        sys.exit(130)
