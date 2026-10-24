# EliCloud `email` 服务

EliCloud 的通用邮箱确认服务。内部服务调用它发起一次邮箱验证；邮件里只有一个确认按钮，用户点击即完成确认；超过超时时间未点击则自动视为拒绝。`email` 服务在批准或超时拒绝时，向调用方提供的内部 callback URL POST 带 HMAC 签名的结果。

- 服务名 / 容器名：`email`
- 对外前缀：`/email`（由 Caddy 剥离）
- 版本：全部接口在 `/v1`
- 存储：自己的 SQLite（`/data/email.db`），不共享、不 join `sso`/`mc`
- 部署：`ghcr.io/elicloudorg/email:prod`，Docker Compose，单机

## 1. 流程

```text
EliCloud 内部服务                    email 服务                       用户
  │ POST /v1/verify (email, purpose,  │
  │  callback_url, metadata, content) │
  │ ─────────────────────────────────► │ 建请求、发确认邮件（一个按钮）
  │ ◄── 202 {id, status:pending} ────── │
  │                                    │  邮件按钮 /v1/confirm?token=...
  │                                    │ ──────────────────────────────► 点击
  │                                    │ 立即 approved / 超时 denied
  │ ◄── POST callback_url（HMAC 签名）─ │ 状态落库 + 投递回调
  │     {request_id, status, reason}   │
```

- **没有网页确认页**：邮件里的按钮直接指向 `/v1/confirm?token=...`，GET 即确认。
- **超时 = 拒绝**：超过 `VERIFICATION_TTL_SECONDS` 未点击，后台 worker 把请求置为 `denied`，回调 `reason=timeout`。
- 安全提示：直接 GET 确认可能被极少数邮件安全扫描器预取。服务用一次性 token + 短超时做缓解，但无法像“网页二次点击”那样完全消除；这是本设计的明确取舍。

## 2. 接口

| 方法 | 服务内路径 | 生产对外路径 | 访问控制 |
|---|---|---|---|
| POST | `/v1/verify` | `/email/v1/verify` | 私网来源 + `Authorization: Bearer <EMAIL_INTERNAL_TOKEN>` |
| GET | `/v1/verify/{id}` | `/email/v1/verify/{id}` | 同上 |
| GET | `/v1/confirm?token=...` | `/email/v1/confirm?token=...` | 公开，一次性 token；GET 即确认 |
| GET | `/v1/healthz` | `/email/v1/healthz` | 公开 |

服务代码里没有 `/email` 前缀（网关负责剥离）；Docker 网络内可直接调 `http://email:8000/v1/verify`。

### 2.1 `POST /v1/verify`

```json
{
  "email": "alice@example.com",
  "purpose": "register",
  "source": "sso",
  "callback_url": "http://sso:8000/internal/email/verified",
  "metadata": {"user_id": "user_0001"},
  "expires_in": 900,
  "content": {
    "mode": "template",
    "template": "register",
    "variables": {"app_name": "EliCloud"}
  }
}
```

- `purpose` 可选，默认 `generic`；`source`、`metadata`、`content`、`expires_in` 可选。
- `expires_in` 是**确认超时**，范围 60–86400 秒，默认 900；超时未点击 = 拒绝。
- 成功 `202`：

```json
{"id": "ev_...", "status": "pending", "expires_in": 900, "expires_at": "2026-10-24T09:15:00Z"}
```

- 错误统一为 `{"error": "...", "error_description": "..."}`：
  - `400 invalid_request` / `invalid_callback_url` / `callback_not_allowed` / `invalid_content` / `metadata_too_large`
  - `401 unauthorized`、`429 too_many_pending` / `rate_limited`、`502 email_delivery_failed`、`503 internal_api_disabled`
- `callback_url` 的 host 必须在 `CALLBACK_ALLOWED_HOSTS` 白名单内，只允许 http/https，拒绝 userinfo/fragment。

### 2.2 `GET /v1/verify/{id}`

```json
{
  "id": "ev_...",
  "status": "approved",
  "email": "alice@example.com",
  "purpose": "register",
  "source": "sso",
  "metadata": {"user_id": "user_0001"},
  "created_at": "2026-10-24T09:00:00Z",
  "expires_at": "2026-10-24T09:15:00Z",
  "decided_at": "2026-10-24T09:01:00Z",
  "decided_by": "user",
  "delivery": {
    "status": "delivered",
    "attempts": 1,
    "last_status_code": 200,
    "last_error": null,
    "delivered_at": "2026-10-24T09:01:05Z"
  }
}
```

`delivery.status`：`pending` / `delivered` / `failed`；`decided_by`：`user`（点击确认）/ `timeout`（超时拒绝）。

## 3. 邮件内容：两条路

### A. 内置模板 `content.mode="template"`

- 内置 `default`、`register` 两套 HTML + 纯文本模板。
- 模板里**有且只有一个确认按钮**（HTML 一个 `<a>`；纯文本一个链接），服务会校验渲染结果里链接恰好出现一次。
- `template` 省略时：purpose 有同名模板就用它，否则 `default`；未知模板 400。
- 系统变量：`link`、`email`、`purpose`、`purpose_label`、`source`、`expires_minutes`、`request_id`、`app_name`。
- 调用方 `variables` 只能补充自定义变量，不能覆盖系统变量；HTML 用 Jinja2 自动转义。
- 邮件正文会写明“超时未确认将视为拒绝”。

### B. 自定义 `content.mode="custom"`

```json
{
  "mode": "custom",
  "subject": "请确认你的 EliCloud 邮箱",
  "text": "点击 {{link}} 完成确认，{{expires_minutes}} 分钟内有效。",
  "html": "<p>点击 <a href=\"{{link}}\">这里</a> 完成确认。</p>",
  "variables": {"app_name": "EliCloud"}
}
```

- `subject` 必填（≤200 字符，禁止 CR/LF）；`text` / `html` 至少一个。
- **`text` / `html` 各自必须且只能出现一次 `{{link}}`**；`subject` 不允许包含 `{{link}}`。这样邮件里只有一个确认按钮/链接。
- **只做 `{{name}}` 字面替换，不用 Jinja 渲染自定义内容**；未知占位符 400；系统占位符不可被 variables 覆盖。
- HTML 变量做 HTML 转义；`link` 作为 URL 插入；text 原样替换。
- 上限：text 20KB、html 50KB、variables ≤20 个且值 ≤1KB。
- 自定义 HTML 视为可信内部调用方内容；不要放第三方不可信 HTML。
- 只存 `content_mode` / `template_name`，**不存正文**。

## 4. 一键确认与 Callback

- 邮件里只有一个按钮，指向 `GET /v1/confirm?token=...`；**GET 即确认**，不再有网页表单或 CSRF。
- 点击后原子地把 `pending` 改为 `approved`（`decided_by=user`），返回一个只读结果页；重复点击是幂等的，不会重复 callback。
- 超过 `expires_at` 未点击：后台 worker（或用户此时才点链接）把请求置为 `denied`（`decided_by=timeout`），并按配置回调 `reason=timeout`。
- Callback body：

```json
{
  "event": "email.verification",
  "request_id": "ev_...",
  "status": "approved",
  "reason": "user_confirm",
  "decided_by": "user",
  "email": "alice@example.com",
  "purpose": "register",
  "source": "sso",
  "metadata": {"user_id": "user_0001"},
  "verified_at": "2026-10-24T09:00:00Z"
}
```

- 超时拒绝时 `status=denied`、`reason=timeout`、`decided_by=timeout`、`verified_at` 为超时时刻。

- 请求头：
  - `X-EliCloud-Event: email.verification`
  - `X-EliCloud-Timestamp: <unix seconds>`
  - `X-EliCloud-Delivery-Id: dl_...`
  - `X-EliCloud-Signature: sha256=<hex HMAC_SHA256(secret, timestamp + "." + raw_body)>`
  - `Idempotency-Key: <request_id>:<status>`
- At-least-once；2xx 成功；其余/超时按 `0s, 30s, 2m, 10m, 1h, 6h` 重试，最多 6 次；耗尽后 `delivery=failed`，状态查询可见。
- 接收方验签示例：

```python
import hashlib, hmac, time

def verify(secret: str, timestamp: int, body: bytes, signature: str) -> bool:
    if abs(time.time() - timestamp) > 300:
        return False
    message = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))
```

## 5. 数据与安全

- `verification_requests`：id、email、email_lower、purpose、source、callback_url、metadata、content_mode、template_name、token_hash(unique)、status、failure_reason、created_at、expires_at、decided_at、decided_by、decided_ip。
- `callback_deliveries`：id、request_id、status、payload、callback_url、attempts、next_attempt_at、last_attempt_at、last_status_code、last_error、delivered_at、failed_at、created_at；`UNIQUE(request_id, status)`。
- SQLite WAL + busy_timeout；单 uvicorn worker；后台线程负责超时拒绝、callback 投递、`RETENTION_DAYS=30` 清理。
- 确认 token 只存 SHA-256，一次性；页面/日志/回调不回显。
- 内部接口 Bearer 常量时间比较；未配置 token 时 fail closed（503）。
- 确认 GET 按 `X-Real-IP` 限流；响应头 `no-store` / `no-referrer` / `DENY` / CSP。
- 日志不记 query、token、完整 email；`uvicorn --no-access-log`；只有 `MAIL_MODE=log` 才打印确认链接（仅开发）。
- `CALLBACK_ALLOWED_HOSTS` 防 SSRF；禁止 redirect。
- **已知取舍**：直接 GET 确认无法完全防住邮件安全扫描器的链接预取；一次性 token + 短超时是缓解手段。若以后不能接受该风险，需要改回“网页二次确认”或 POST，但那与当前“邮件里一个按钮”的要求冲突。

## 6. 本地运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
cp .env.example .env.local      # 按需修改；.env.local 已被 gitignore
uvicorn app.main:app --host 127.0.0.1 --port 8010
```

本地默认 `MAIL_MODE=log`：确认链接会写进日志，适合联调；生产用 smtp。

### 6.1 本地真实发信（借用服务器 Postfix）

服务器上已有 Postfix + Resend 代发（`relayhost=[smtp.resend.com]:587`）。本机可通过临时 SSH 隧道发真实邮件：

```bash
# 本机（DSH 的 server_tunnel 或 ssh -L）
ssh -N -L 2525:127.0.0.1:25 deploy@146.56.237.33
```

`.env.local`：

```ini
MAIL_MODE=smtp
SMTP_HOST=127.0.0.1
SMTP_PORT=2525
SMTP_STARTTLS=false
MAIL_FROM=no-reply@elipese.cloud
```

启动服务后：

```bash
EMAIL_INTERNAL_TOKEN=... CALLBACK_SIGNING_SECRET=... \
  python scripts/e2e_local.py --to you@example.com
```

脚本会在本机 9001 起 callback 接收器，真实发信；你在**同一台电脑的浏览器**点邮件里唯一的确认按钮即可（GET 直接确认，没有网页二次点击），脚本随后验签、查状态。

### 6.2 生产 SMTP

生产容器不经过宿主 Postfix，直接连 Resend：

```ini
MAIL_MODE=smtp
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_USERNAME=resend
SMTP_PASSWORD=<Resend API key，从服务器 /etc/postfix/sasl_passwd 安全提取，不要提交/打印>
SMTP_STARTTLS=true
MAIL_FROM=no-reply@elipese.cloud
```

## 7. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8010` | 对外地址；确认链接由它派生 |
| `DATABASE_URL` | `sqlite:////data/email.db` | 本服务自己的 SQLite |
| `EMAIL_INTERNAL_TOKEN` | 空 | 内部 Bearer token；空 = 内部接口 503 |
| `CALLBACK_SIGNING_SECRET` | 空 | callback HMAC 密钥 |
| `CALLBACK_ALLOWED_HOSTS` | `127.0.0.1,localhost` | callback host 白名单 |
| `MAIL_MODE` | `log` | `log` / `smtp` |
| `SMTP_*` | — | smtp 模式必填 host/port；账号密码可选 |
| `MAIL_FROM` / `MAIL_FROM_NAME` | `no-reply@elipese.cloud` / `EliCloud` | 发件人 |
| `VERIFICATION_TTL_SECONDS` | `900` | 确认超时；超时未点击 = 拒绝 |
| `MAX_ACTIVE_PER_EMAIL` | `3` | 同邮箱待确认上限 |
| `CREATE_COOLDOWN_SECONDS` | `60` | 同邮箱创建冷却 |
| `CONFIRM_RATE_LIMIT_PER_MINUTE` | `10` | 确认 GET 限流 |
| `CALLBACK_MAX_ATTEMPTS` | `6` | 最多投递次数 |
| `CALLBACK_RETRY_SCHEDULE` | `0,30,120,600,3600,21600` | 首次立即，失败后间隔 |
| `CALLBACK_ON_TIMEOUT` | `true` | 超时拒绝是否回调调用方 |
| `RETENTION_DAYS` | `30` | 终态数据保留天数 |
| `TRUST_PROXY_HEADERS` | `true` | 生产由 Caddy 注入 X-Real-IP |
| `LOG_LEVEL` | `info` | 日志级别 |

## 8. 与 SSO 的关系

本服务只负责“邮箱是不是这个人的、用户是否确认”，不修改 `sso` 仓库。SSO 注册侧后续应：

1. 调 `POST /v1/verify`，带 `purpose=register`、`callback_url`（SSO 内部端点）和 `metadata`（如 `user_id`）；
2. 在自己的内部回调端点验 HMAC、按 `request_id` 幂等处理；
3. 只有 `status=approved` 且 `reason=user_confirm` 才把 `email_verified=true` 写回 SSO 用户表；`status=denied`（`reason=timeout`）必须保持未验证。

在 SSO 这一步落地前，本服务可独立用测试调用方验证全链路。

## 9. 部署

见 [README-deploy.md](README-deploy.md)。生产上线由维护者手动推送 `prod` 或手动 `workflow_dispatch`；本仓库的 `main` 只跑 CI。
