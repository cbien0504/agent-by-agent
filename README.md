# Agent demo với Anthropic API

Một AI agent dòng lệnh nhỏ để học cách agent hoạt động. Bạn đưa nhiệm vụ bằng ngôn ngữ tự nhiên. Agent tự lên kế hoạch, dùng 4 công cụ làm việc trong thư mục `sandbox/`, tự kiểm tra kết quả rồi báo lại. Code chỉ dùng SDK chính thức `anthropic` và thư viện chuẩn, không dùng framework agent nào.

## Cài đặt

Cần Python 3.10 trở lên (đã chạy thử trên 3.10.13).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Đặt API key

Agent chỉ đọc key từ biến môi trường `ANTHROPIC_API_KEY`. Key không nằm trong code, không bị in ra và không được ghi vào trace. Nếu thiếu key, agent báo lỗi rồi dừng.

```bash
export ANTHROPIC_API_KEY='sk-ant-...'   # hoặc để dòng này trong .env (đã gitignore) rồi: source .env
export MODEL=claude-sonnet-5-5          # tuỳ chọn; đây cũng là model mặc định
```

## Chạy

```bash
python setup_demo.py                  # tạo sandbox/sales.csv (30 dòng) và in đáp án đúng
python agent.py --file demo_task.md   # chạy nhiệm vụ demo
python agent.py "Tạo file hello.txt chứa lời chào"
python evals.py                       # eval: 3 nhiệm vụ × 3 lần (--runs N để đổi)
python -m unittest test_tools -v      # test các tool, không cần API key
```

Trong terminal, sau mỗi câu trả lời bạn có thể gõ tiếp: trả lời câu hỏi của agent hoặc giao thêm việc. Agent vẫn nhớ ngữ cảnh. Nhấn Enter để thoát.

Màn hình in ra từng bước:
- `[nói]` là câu model viết ra.
- `[nghĩ]` là tóm tắt suy nghĩ của model (có khi bằng tiếng Anh).
- `[tool]` là một lệnh gọi công cụ kèm kết quả.

```
[Bước 1] đang gọi model... 2.7s · token vào 4 · ghi cache 2,091 · đọc cache 0 · ra 87 (tổng 2,182)
  [nói] Tôi sẽ xem dữ liệu trong sandbox trước, rồi mới phân tích.
  [tool] read_file(path='sales.csv')
         → OK sau 0.00s: order_id,date,product,quantity,unit_price 1,2026-01-03,Laptop,3,18500000 …
```

## Các thành phần và khái niệm tương ứng

### 1. Vòng lặp: `run_agent()` trong `agent.py`

Agent chỉ là một vòng lặp quanh API. Mọi quyết định (đọc gì, tính gì, khi nào xong) đều do model đưa ra; code chỉ làm theo và lặp lại:

1. `call_model()` gửi `system` + `tools` + `messages` lên `client.messages.create`.
2. Nếu `stop_reason == "tool_use"`: chạy các khối `tool_use`, gửi **tất cả** `tool_result` trong **một** message `user`, rồi quay lại bước 1.
3. Nếu `end_turn`: in câu trả lời cuối. Nếu gặp `max_tokens`, `refusal`, lỗi API hoặc chạm giới hạn: dừng và nói rõ vì sao.

Bản kế hoạch agent in ra là do model tự viết, theo quy tắc trong `SYSTEM_PROMPT`; code không có bước "lập kế hoạch" nào.

### 2. Tool: `tools.py`

- **Hai nửa của một tool:** một hàm Python làm việc thật, và một JSON schema trong `TOOL_SCHEMAS`. Model chỉ thấy schema (tên, mô tả, tham số), nên mô tả phải rõ ràng. Ví dụ, mô tả của `run_python` ghi rõ "không có pandas" để model khỏi thử.
- **Lỗi giúp agent tự sửa:** `execute_tool()` không bao giờ làm sập chương trình. Mọi lỗi được trả cho model với `is_error: true`, kèm lời nhắn "sai gì, nên thử gì".
- **Hàng rào sandbox:** `safe_path()` chặn đường dẫn tuyệt đối, `..` và `~`. Sau khi `resolve()`, nó kiểm tra lại đường dẫn để bắt cả symlink trỏ ra ngoài.
- **Chạy song song:** `run_tool_calls()` (trong `agent.py`) xử lý các tool trong một lượt như sau:
  - Nếu tất cả đều chỉ đọc, chúng chạy **song song**.
  - Nếu có `write_file` hoặc `run_python`, chúng chạy **lần lượt** theo thứ tự model gọi.

  Vì sao cần tuần tự: trong eval, model từng gọi `write_file(stats_utils.py)` và `run_python(import stats_utils…)` trong cùng một lượt. Nếu chạy song song, test có thể chạy trên file cũ.

### 3. Context: danh sách `messages`

- **Bộ nhớ:** API không nhớ gì giữa các lần gọi. Mỗi bước gửi lại toàn bộ hội thoại, nên `messages` chính là bộ nhớ của agent.
- **Chỉ nối thêm:** phản hồi của model được nối vào nguyên vẹn, kể cả khối `thinking`. Lịch sử không bao giờ bị sửa, vì Sonnet 5.5 yêu cầu vậy khi dùng thinking.
- **Phần cố định:** `SYSTEM_PROMPT` gồm vai trò, quy tắc và mẫu câu trả lời cuối. Các quy tắc là: làm từng bước nhỏ, kiểm tra trước khi báo xong, hỏi lại khi mơ hồ, chỉ làm trong sandbox.
- **Prompt caching:** request có 3 điểm cache: tool cuối, system prompt, và `cache_control` ở cấp request cho phần hội thoại đang dài ra.
  - Trên màn hình, số "đọc cache" tăng dần qua các bước, còn "token vào" (phần tính giá gốc) chỉ còn vài token.
  - Đọc từ cache rẻ hơn 10 lần: lần chạy eval hết 0,075 USD, nếu không cache sẽ là 0,167 USD.
- **Hỏi tiếp:** CLI dùng lại cùng danh sách `messages`, nên agent hiểu được các câu như "thêm dòng vào file vừa tạo".

### 4. Trace: `traces/<timestamp>.jsonl`

Mỗi dòng là một sự kiện JSON:

- `start`: nhiệm vụ, model, các giới hạn.
- `step`:
  - số thứ tự bước, `stop_reason`, thời gian chạy;
  - token: `input`, `cache_write`, `cache_read`, `output`;
  - lời nói và suy nghĩ của model;
  - từng tool đã gọi: `name`, `input`, `output` đã cắt, `is_error`, `seconds`.
- `end`: trạng thái, tổng số bước, tổng token, câu trả lời.

Lệnh xem trace: `python -m json.tool --json-lines traces/<file>.jsonl`.

Trace trả lời câu hỏi "agent đã làm gì và vì sao". Chuyện `write_file` và `run_python` nằm chung một lượt ở mục 2 cũng được phát hiện nhờ đọc trace.

### 5. Eval: `evals.py`

Mỗi nhiệm vụ có một sandbox sạch riêng trong `eval_runs/`, một `prompt`, và một hàm `check` chấm đúng/sai **bằng code**:

| Nhiệm vụ | Agent phải làm | Cách chấm |
|---|---|---|
| `primes` | ghi 15 số nguyên tố đầu tiên vào `primes.txt` | so khớp từng dòng |
| `sales_top` | tìm sản phẩm có doanh thu cao nhất mỗi tháng, ghi vào `top_products.json` | so với đáp án tính từ `setup_demo.py` |
| `fix_median` | sửa hàm `median` bị lỗi trong `stats_utils.py` | chạy hàm trên 5 ca kiểm thử trong tiến trình riêng |

Model không tất định, nên mỗi nhiệm vụ chạy nhiều lần. Muốn thêm nhiệm vụ, thêm một mục vào `TASKS`.

### Giới hạn an toàn

- **Đầu `agent.py`:** `MAX_STEPS = 15` lượt gọi model, `TOKEN_BUDGET = 200_000` token, `TOOL_TIMEOUT_S = 30`, `MAX_TOKENS = 16_000` cho mỗi phản hồi, `EFFORT = "medium"`.
- **Đầu `tools.py`:** `RUN_PYTHON_TIMEOUT_S = 10`, cùng các giới hạn cắt bớt output.

## ⚠️ `run_python` không phải sandbox bảo mật

Những gì đã làm để hạn chế `run_python`:
- thư mục làm việc là `sandbox/`;
- biến môi trường tối giản, **không chứa API key**;
- stdin đóng;
- timeout 10 giây.

Dù vậy, code do model viết vẫn chạy với quyền của bạn, nên **vẫn có thể** đọc/ghi file bên ngoài (kể cả `.env`) và gọi mạng. `safe_path()` chỉ bảo vệ 3 tool làm việc với file. Quy tắc trong system prompt chỉ là thoả thuận với model, không phải rào chắn. Muốn cách ly thật, hãy chạy agent trong container không có mạng, chỉ mount thư mục sandbox.

## Kết quả chạy thử

Ngày 2026-11-04, model `claude-sonnet-5-5`, effort `medium`.

- **Demo:** đúng cả 3 tháng (Phone 89,1 triệu; Tablet 86,4 triệu; Laptop 111 triệu VND).
  - Hết 4 bước, 14.058 token, 12 giây, khoảng 0,025 USD.
  - Agent tự kiểm tra `report.md` bằng một đoạn code tính lại độc lập, và không mắc hai "bẫy" cài trong dữ liệu.
- **Eval:** 9/9 đúng, trung bình 2,7 bước và 7.001 token mỗi lần chạy, tổng khoảng 0,075 USD.

```
Nhiệm vụ        Thành công   Bước TB    Token TB
primes          3/3 (100%)       2.0       4,499
sales_top       3/3 (100%)       3.0       8,875
fix_median      3/3 (100%)       3.0       7,630
TỔNG            9/9 (100%)       2.7       7,001
```

Ba nhiệm vụ này còn dễ. Muốn eval giúp phân biệt thay đổi tốt hay dở, hãy thêm nhiệm vụ khó hơn, ví dụ dữ liệu bẩn hoặc yêu cầu nhiều bước.

**Ghi chú:** tham số `thinking` (adaptive) và `effort` trong `call_model()` dành cho các model Claude thế hệ 5. Nếu đổi `MODEL` sang model cũ hơn mà API báo lỗi 400, hãy bỏ hai tham số này.
