# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity agent | claimed/candidate order IDs | Gọi `get_order`, xác nhận order ID trả về khớp candidate, reject candidate không hợp lệ | `get_order` | `entity-agent -> coordinator`, entity resolution status |
| Customer agent | customer hint, case scope | Lấy lịch sử customer và related orders | `get_customer_history` | Customer context, evidence ref |
| Coordinator | input case, entity result | Điều phối task, giới hạn query budget, giữ case scope và tổng hợp output | Không tự gọi ngoài permission map | Task assignments và specialist handoffs |
| Order agent | resolved order ID | Lấy item, seller và entity IDs | `get_order_items`, `get_sellers` | Affected entities |
| Product agent | resolved order ID | Lấy product/category context khi scope yêu cầu | `get_product_context` | Product evidence |
| Shipment agent | resolved order ID | Phân tích delivery timestamps, shipping limits và shipment events | `get_shipment_summary` | Shipment verdict |
| Payment agent | resolved order ID | Đối soát payment rows với captured lifecycle events | `get_order_payments`, `get_payment_timeline` | Payment verdict và totals |
| Refund agent | resolved order ID | Đọc refund lifecycle, không tự tạo refund event | `get_refund_timeline` | Refund status/evidence |
| Policy agent | policy version | Lấy rule, status, action, amount và responsible parties | `get_policy` | Policy decision input |
| Conflict resolver | specialist results | Chọn source precedence, biểu diễn conflict, map claims | Không gọi MCP | Primary issue, conflicts, actions |
| Verifier | assembled report, evidence refs | Kiểm tra invariant trước finalize | Không gọi MCP | `verification_completed` hoặc fail-fast |

Permission được thực thi trong workflow bằng map actor/tool. Tool discovery
không cấp quyền tự động cho actor.

## 3. Entity resolution và A2A protocol

### Resolution

1. Tập candidate là hợp của `candidate_order_ids` và claimed order ID.
2. Mỗi candidate chỉ hợp lệ nếu `get_order` trả về data có đúng `order_id`.
3. Claimed order được ưu tiên khi hợp lệ; nếu chỉ có một order hợp lệ thì status
    là `resolved`.
4. Không có order hợp lệ tạo `not_found`; nhiều order hợp lệ tạo `ambiguous`.
5. Candidate không được chọn được đưa vào `rejected_candidates`; không có
    fallback bằng cách đoán ID.

Confidence là bounded value trong `[0, 1]`: resolution duy nhất là `0.95`,
resolution không chắc chắn là `0.35`. Đây là confidence của workflow, không
phải xác suất bí mật.

### Handoff envelope

Code dùng trace event làm observable A2A envelope tối giản:

```text
case_id + actor + target + decision_code + tool_name + evidence_refs
```

`case_id` là correlation key. Handoff chỉ tiến về coordinator/specialist/verifier;
không có vòng lặp agent-to-agent. Evidence ref được giữ nguyên từ MCP response.
Nội dung suy luận riêng không được ghi vào trace.

## 4. Evidence và conflict lifecycle

Mỗi response đi qua `EvidenceGateway`, nơi public
`mcp-evidence-response-v1.schema.json` được validate. Workflow sau đó:

1. Lưu `evidence_ref` trong bộ nhớ của case.
2. Emit `tool_result_consumed` với đúng actor, tool và evidence ref.
3. Chỉ truyền evidence ref vào claim/output/trace, không sửa hoặc tự tạo ref.
4. Dùng cache theo `(tool_name, arguments)` trong cùng case để tránh call lặp.
5. Không cho evidence của case này xuất hiện trong case khác.

Precedence hiện tại:

- Order row là nguồn chính cho order identity/status.
- Payment lifecycle là nguồn chính khi payment rows mâu thuẫn với captured events.
- Shipment event và delivery timestamps được dùng để phân loại delay.
- Policy là nguồn chính cho recommended action, amount và responsible party.

Conflict được biểu diễn trong `data_conflicts` với `field`, `sources`,
`selected_source` và `resolution_code`. Nếu không đủ evidence, workflow dùng
`insufficient_evidence`, không điền dữ liệu phỏng đoán.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/network/server error | Tối đa 2 retry, exponential backoff | Bỏ evidence của tool, chuyển verdict về thiếu evidence nếu cần | `handoff/evidence_unavailable` |
| Entity not found | 0 | Reject candidate | `handoff/entity_not_found` |
| Entity ambiguous | 0 | Không tự chọn nếu không có claimed order ưu tiên | `handoff/entity_ambiguous` |
| Source conflict | 0 | Chọn source theo precedence, lưu `data_conflicts` | `policy_decided` |
| Invalid specialist result/schema | 0 | Không dùng response invalid | `handoff/evidence_unavailable` |
| Query budget exhausted | 0 | Không gọi thêm MCP, finalize với evidence hiện có | `handoff/query_budget_exhausted` |

Giới hạn hiện tại là `16` MCP attempts cho một case. Retry được tính vào
budget; chỉ response thành công mới được cache. Các call là idempotent reads,
không có mutation/retry side effect.

## 6. Verification invariants

Trước khi trả output, verifier kiểm tra:

- Evidence refs trong report đều thuộc evidence của đúng case.
- Resolved order nằm trong `affected_entities.order_ids`.
- Một order không thể vừa resolved vừa rejected.
- Claim evidence refs thuộc evidence hiện tại.
- Assessment, resolution và claim confidence nằm trong `[0, 1]`.
- Payment totals không âm.
- `refundable_total_brl = max(captured - refunded, 0)` khi đủ totals.
- Tổng `refund_lines.amount_brl` bằng `recommended_refund_brl`.
- Case `no_action` không được đề xuất refund.
- Phải có ít nhất một `resolution_action`.
- Output sau đó vẫn được public JSON Schema validator kiểm tra ở CLI.

Verifier không tự chữa dữ liệu sai; invariant failure là fail-fast để không tạo
artifact có vẻ hợp lệ nhưng mâu thuẫn nội bộ.

## 7. Reproducibility

- Runtime: Python trong `.venv`, workflow thuần async Python.
- Không dùng model hoặc random seed trong decision path.
- MCP calls tuần tự để giữ thứ tự trace và audit ổn định.
- Timeout/network retry: tối đa 2 lần, backoff `0.2s`, `0.4s`.
- Query budget: 16 attempts/case.
- Cache: chỉ trong memory của một lần `solve_case()`.
- Validation commands:

```powershell
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Không ghi `COMPETITION_TEAM_API_KEY`, `GEMINI_API_KEY` hoặc secret khác vào
source, trace, output, architecture document hay submission ZIP.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | TODO | TODO | TODO | TODO |
| Coordinator | TODO | TODO | TODO | TODO |
| Order/product | TODO | TODO | TODO | TODO |
| Shipment | TODO | TODO | TODO | TODO |
| Payment/refund | TODO | TODO | TODO | TODO |
| Policy | TODO | TODO | TODO | TODO |
| Conflict resolver | TODO | TODO | TODO | TODO |
| Verifier | TODO | TODO | TODO | TODO |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Mô tả cách xếp hạng/reject candidate, confidence threshold, message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Không trace nội dung suy luận riêng.

## 4. Evidence và conflict lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, chọn source theo policy, biểu diễn unresolved conflict, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Entity not found/ambiguous | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
