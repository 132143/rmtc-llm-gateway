# 官方网关接入（2026-10-28 答疑后的实现口径）

| 项 | 内容 |
|---|---|
| 依据 | 主办方 2026-10-28 群内答疑 + 官方请求示例 |
| 代码位置 | `rmtc_client/llm_client.py`（客户端）｜`contestant/bot_node.py`（入口） |
| 影响面 | **只有 AI 链路**；策略、决策顺序、IO/QoS/回执零改动 |

## 一、官方给了什么约束（逐条对应我们的实现）

| # | 官方口径 | 我们的实现 | 验证 |
|---|---|---|---|
| 1 | 网关**除 model 外**全部字段直接转发，无字段白名单 | 我们只发 `messages / max_tokens / response_format / thinking`，**不填 model** | `tests/test_client.py::TestGatewayPayload::test_payload_shape` 断言请求体没有 `model` |
| 2 | 思考模式**默认开启**；关闭方式为顶层 `thinking={"type":"disabled"}` | `ClientConfig.disable_thinking=True` → 每次都带该字段 | 同上断言 `thinking == {"type":"disabled"}` |
| 3 | 关闭思考后延迟 **0.78–0.99 s** | `timeout_s=4.0`（异步调用，不堵 500 ms 动作窗） | 真机复核时打印 p50/p95 |
| 4 | `max_tokens` 建议 **512–1024** | 默认 **512**（原来 200，会把 `avoid_cells` 列表截断成坏 JSON） | 断言 `max_tokens == 512` |
| 5 | **必须使用统一默认模型**，选手不得填写（禁止私自调用其他模型） | `bot_node` 不读 `RMTC_MODEL`；只有离线直连 `api.deepseek.com` 录制时才显式传 | 断言默认请求无 `model` |
| 6 | 支持 `response_format` / `temperature` 等参数 | `response_format={"type":"json_object"}`（配合 prompt 里出现 "json"）；`temperature` 不转发默认值 | 被拒时自动回退（见下） |

## 二、两个自动回退（防"因为一个可选字段丢掉 15 分"）

规则 §10.2：本场若无真实 AI 成功调用，任务分 M=0（最多丢 15 分）。所以任何一个**可选字段**被上游拒绝，
都必须能自动降级重试，而不是整场放弃 AI。

| 触发 | 处理 | 日志 |
|---|---|---|
| 400 且消息含 `thinking` | 本场改发默认思考模式（不再发该字段），**立刻重试一次** | `warn: 上游拒绝 thinking 字段…本场改为默认思考模式` |
| 400 且含 `json_object` + `response_format` | 本场关掉 JSON 模式，重试一次 | `warn: 网关拒绝 json_object 输出模式…` |
| 其它 4xx/5xx | 按协议错误码分类：`invalid_token`/`quota_exceeded` 停用；`rate_limit`/5xx 退避 | `error/warn` |

> 400 不产生 2xx，不消耗网关的成功配额（规则 §11.3 按成功调用记账），所以回退重试不会白烧额度。

## 三、检录时怎么自证（截图用）

第一次调用时客户端会打印**一行**（不含令牌与提示词）：

```
[ai:info] AI 请求参数：thinking=disabled，json_mode=on，max_tokens=512，timeout=4.0s
```

这一行就是"我们按要求关闭了思考、预算合理"的证据，配合裁判端 `success_count ≥ 1` 一起截。

## 四、测试

| 用例 | 断言 |
|---|---|
| `test_payload_shape` | `thinking={"type":"disabled"}`、`max_tokens=512`、**无 `model`**、`response_format=json_object` |
| `test_thinking_rejected_falls_back_once` | 首次 400（含 thinking）→ 第二次请求不带该字段 → 成功；`thinking_mode` 变 `default` |
| `test_env_can_keep_thinking_on` | `disable_thinking=False` 时不发 thinking；显式给 model 时才发 |
| `test_missing_token_raises_*` | 令牌/网关缺失 → 构造即报错（防静默 401 丢资格） |
