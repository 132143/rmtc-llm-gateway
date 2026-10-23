# 协议对照表：每条要求落在哪一行代码

依据《RMTC 机甲大师图灵对决赛通信协议 v1.1》§5「LLM 网关（OpenAI 兼容）」与 §7「联调与正式成绩」。
"测试"列的用例都能用 `python3 run_tests.py -v` 单独复现（`python3 -m unittest tests.test_gateway.TestRequestShape.test_invalid_json -v`）。

## 1. 端点

| 协议原文 | 代码 | 测试 |
|---|---|---|
| `<GATEWAY>/healthz  GET 服务可用性检查` | `rmtc_gateway/server.py: Handler.do_GET` | `TestHealthAndAuth.test_healthz_ok_without_token` |
| `<GATEWAY>/v1/chat/completions  POST OpenAI 兼容对话接口` | `Handler.do_POST` | `TestSuccessAndQuota.test_mock_success_shape` |

## 2. 请求与身份

| 协议原文 | 代码 | 测试 |
|---|---|---|
| `Authorization: Bearer <本队令牌>（必填）` | `Handler._authenticate` | `test_missing_token` / `test_invalid_token` / `test_malformed_authorization_is_missing_token` |
| `messages` 必须是非空数组 | `Handler._parse` | `test_empty_messages` / `test_missing_messages` |
| 未提供有效 `model` 时使用主办方默认模型 | `Handler._parse` + `upstream.HttpUpstream.complete` | `test_http_upstream_forwards_and_defaults_model` |
| 请求体上限 2097152 字节，超过返回 413 | `Handler._read_body` | `test_payload_too_large` |
| 带 `Transfer-Encoding` 的请求返回 501 | `Handler._read_body` | `test_transfer_encoding_rejected` |
| 响应以完整内容返回，不提供流式增量 | 全链路 `_send_bytes`，客户端从不设 `stream` | `TestClientCalls.test_success`（断言请求体里没有 stream） |

## 3. 响应与错误

| HTTP | type | code | 代码 | 测试 |
|---|---|---|---|---|
| 401 | authentication_error | missing_token | `errors.missing_token` | `test_missing_token` |
| 401 | authentication_error | invalid_token | `errors.invalid_token` | `test_invalid_token` |
| 404 | invalid_request_error | not_found | `errors.not_found` | `test_unknown_path` |
| 400 | invalid_request_error | invalid_content_length / incomplete_body | `errors.*` | `test_missing_content_length` / `test_duplicate_content_length` / `test_incomplete_body` |
| 413 | invalid_request_error | payload_too_large | `errors.payload_too_large` | `test_payload_too_large` |
| 429 | insufficient_quota | quota_exceeded | `errors.quota_exceeded` | `test_quota_exhaustion` |
| 429 | rate_limit_error | rate_limit / concurrency_limit | `errors.rate_limit` / `errors.concurrency_limit` | `test_rate_limit_per_ip` / `test_concurrency_limit` |
| 503 | server_error | audit_unavailable | `errors.audit_unavailable`（已定义，仅在审计写入失败时按需抛出） | — |
| 501 | invalid_request_error | unsupported_transfer_encoding | `errors.unsupported_transfer_encoding` | `test_transfer_encoding_rejected` |
| 502 | upstream_error | upstream_unavailable | `errors.upstream_unavailable` | `test_upstream_unreachable_is_502` / `test_upstream_error_status_is_502` |
| 504 | upstream_error | upstream_timeout | `errors.upstream_timeout` | `test_upstream_timeout_is_504_and_free` |

补充（协议未点名、OpenAI 兼容接口必需）：`invalid_json`、`invalid_messages`、`stream_unsupported`，
同样 400 + `invalid_request_error`。

## 4. 请求频率与配额

| 协议原文 | 代码 | 测试 |
|---|---|---|
| 每个来源 IP 每 10 秒最多 100 次 POST | `limits.RateLimiter` | `test_rate_limit_per_ip` |
| 每个席位令牌最多同时存在 2 个在途调用 | `limits.TokenState.acquire_slot` | `test_concurrency_limit` |
| 额度按令牌全生命周期累计，不按时间窗重置 | `TokenState.used` | `test_quota_exhaustion` |
| 只有成功（2xx）的转发消耗额度 | `reserve → commit / release` | `test_rejected_request_does_not_consume_quota` |
| 在途调用占名额但不计入成功次数，失败即释放 | `pending` 计数 | `test_upstream_timeout_is_504_and_free`（断言 used/pending/inflight 全归零） |
| `0` 表示不限制 | `TokenState.quota == 0` | `test_quota_zero_means_unlimited` |
| 额度耗尽只能通过 429 `quota_exceeded` 感知 | `healthz` 故意不返回剩余额度 | `test_healthz_does_not_expose_quota_usage` |
| 网关不重试上游请求 | `HttpUpstream.complete` 单次调用 | `test_http_upstream_forwards_and_defaults_model`（断言上游只收到 1 次） |
| 上游超时默认 30 秒 | `Config.upstream_timeout_s` | `test_upstream_timeout_is_504_and_free`（测试里改成 0.2 s） |
| 网关请求读取超时 5 秒 | `Config.read_timeout_s` | `test_incomplete_body` |

## 5. §7 联调与正式成绩（客户端侧）

| 协议原文 | 代码 | 测试 |
|---|---|---|
| 参赛程序宜异步请求并设置本地降级，不能让模型等待阻塞动作窗 | `LlmGatewayClient.ask_async` + `EventUnderstanding` | `TestClientCalls.test_timeout_is_a_value_not_an_exception` |
| 必须同时处理 HTTP 状态码、错误码、正文解析失败与连接异常 | `LlmGatewayClient._on_http_error` / `_parse_success` | `test_broken_response_is_handled` / `test_unreachable_gateway_is_handled` |
| 额度用尽后必须自主降级 | `_FATAL_CODES` → `degraded` | `test_quota_exceeded_degrades_permanently` |
| 429 `rate_limit`/`concurrency_limit` 可退避后重试 | `_BACKOFF_CODES` | `test_rate_limit_backs_off_then_recovers` |
| 随机事件结果必须核验所属场次与当前时效 | `EventUnderstanding.offer / hint` | `test_stale_hint_is_dropped` / `test_new_session_clears_old_hint` |
| 新的 `session_id` 要清空上一场缓存 | `EventUnderstanding.offer` | `test_new_session_clears_old_hint` |

## 6. 刻意没做的（以及为什么）

| 没做 | 原因 |
|---|---|
| 网关里放提示词模板 | 协议：网关不提供任何决策逻辑 |
| 客户端用正则/关键词解析事件 | 协议明令禁止，必须交给大模型 |
| `/healthz` 返回剩余额度 | 协议：网关不提供配额查询与管理接口 |
| 上游失败自动重试 | 协议：网关不重试上游请求 |
| 模拟上游生成"像样"的战术建议 | 避免被误当作离线 AI 使用 |
