# `email` 部署说明

本仓库使用「GitHub Actions 触发 + 服务器自管」的自托管部署框架。

框架只负责**触发和调用**，不关心项目怎么构建、怎么运行；一切具体部署逻辑都在服务器的
`/srv/email/deploy.sh` 里。

> **当前初始化状态**：仓库已推 `main` 并跑 CI；`prod` **尚未推送**，生产服务器**尚未改动**
> （没有 `/srv/email`、没有容器、没有 Caddy 路由）。下面「首次部署」由维护者手动执行。

## 本项目实际信息

| 项 | 值 |
|---|---|
| 技术栈 | Python 3.12（FastAPI + Uvicorn + SQLAlchemy，SQLite） |
| 应用名 | `email` → 远程目录 **`/srv/email`** |
| 仓库 | `EliCloudOrg/email`（public；免费组织 private 仓库配不了分支保护） |
| 部署目标 | `146.56.237.33`（Ubuntu），SSH 用户 `deploy`，端口 `22` |
| CI 命令 | `pip install -r requirements-dev.txt` → `pytest -q` → `docker build -t email:ci .` |
| 镜像 | `ghcr.io/elicloudorg/email:prod` 与 `:sha-<short>`（`DEPLOY_MODE=image`） |
| 部署入口 | `/srv/email/deploy.sh`（`deploy:deploy` 0750，每次由 Actions 上传覆盖） |
| Docker 可见的项目目录 | `/home/deploy/elicloud-email`（compose 项目名 `email`，由 `deploy.sh` 生成） |
| 数据目录 | `/home/docker-admin/elicloud/email/data`（`email.db`，**保持原位、不随部署移动**） |
| 网关网络 | `dsh-nas_dsh-net`（external，dsh-caddy 在其中；容器内 8000，宿主 `127.0.0.1:8010`） |
| 运行时变量 | `/srv/email/app.env`（0600 `deploy:deploy`） |
| 密钥 | Actions Secret `SSH_KEY`（部署私钥）；公钥在 `/home/deploy/.ssh/authorized_keys` |

## ⚠️ 本机环境的三条硬约束

1. **Docker 是 snap 安装的**（`/snap/bin/docker`），snap 沙箱**看不到 `/srv`**。
   任何把 `/srv` 路径交给 docker 的写法都会失败：`-v /srv/...`、`--env-file /srv/...`、`docker build /srv/...`。
   → `deploy.sh` 自己在 `/srv/email/app.env` 读变量，写到 `/home/deploy/elicloud-email/.env`，
   交给 docker 的只有 `/home` 下的路径。
2. **数据留在 `/home/docker-admin/elicloud/email/data`**，不随部署移动。
   容器以 `uid 1002:1003`（宿主机 docker-admin）运行，数据目录由 `deploy.sh` 用一次性容器创建/校正。
3. **`/home/docker-admin` 对 `deploy` 用户不可遍历**（home 目录权限如此，实测）。
   bind mount 由 root 的 docker daemon 完成，所以容器挂载不受影响；但 `deploy.sh` 里不能用
   shell 去 `stat` 数据目录，改用「借容器校验」。

## 部署架构

```
      feature/* ──PR──▶ main ──PR──▶ prod
                         │            │
                    push │            │ push / workflow_dispatch(ref)
                         ▼            ▼
              ┌──────────────────┐  ┌───────────────────────────────────────┐
              │ CI  ci.yml       │  │ Deploy  deploy-prod.yml               │
              │  检出            │  │  ① 构建镜像 → ghcr.io/elicloudorg/email│
              │  安装依赖        │  │     仅当 vars.DEPLOY_MODE == image    │
              │  pytest -q       │  │  ② scp deploy/deploy.sh → /srv/email  │
              │  docker build    │  │  ③ ssh 执行 /srv/email/deploy.sh REF  │
              └──────────────────┘  │  environment: production（人工审批）  │
                                    └──────────────────┬────────────────────┘
                                                       │ SSH（deploy 用户 + 私钥）
                                                       ▼
                                    ┌───────────────────────────────────────┐
                                    │ 服务器  /srv/email/                   │
                                    │   deploy.sh   ← 随仓库版本管理         │
                                    │   app.env     ← 运行时环境变量（600）  │
                                    │  ↓ bash 读 /srv、写 /home              │
                                    │ /home/deploy/elicloud-email/          │
                                    │   .env + docker-compose.yml（生成物）  │
                                    │  ↓ docker compose up -d               │
                                    │ 容器 email ← ghcr.io/elicloudorg/email │
                                    │   /data ← /home/docker-admin/elicloud/ │
                                    │           email/data                  │
                                    │  ↓ 只加入 dsh-nas_dsh-net              │
                                    │ dsh-caddy:                           │
                                    │   /email/v1/confirm|healthz → email   │
                                    │   /email/v1/verify* 仅私网 → email     │
                                    └───────────────────────────────────────┘
```

## 分支模型

| 分支 | 用途 | 合并规则 |
|---|---|---|
| `main` | 集成分支，日常 PR 合入，跑 CI | 需要 PR + CI 通过 |
| `prod` | 发布分支，受保护 | 只允许从 `main` 发 PR 合并；合并即触发部署 |
| `feature/*` | 功能分支 | 从 `main` 拉出，PR 回 `main` |

## 首次部署（维护者手动执行）

> 顺序很重要：先建目录/密钥/变量，再推 `prod`，否则部署会因缺 `/srv/email` 或 `app.env` 失败。

### 1. 服务器初始化（root；本机用有 sudo 的 `ubuntu` 账号）

```bash
scp scripts/bootstrap-server.sh ubuntu@146.56.237.33:/tmp/
ssh ubuntu@146.56.237.33 'sudo bash /tmp/bootstrap-server.sh email'
```

脚本创建 `/srv/email`（`deploy:deploy` 750）、空的 `/srv/email/app.env`，并确认 `deploy` 在 docker 组。

### 2. 安装部署公钥

初始化时已生成部署专用密钥（**不进仓库**）：

- 私钥：`D:\work\projects\EliCloud\.deploy-keys\email_deploy_ed25519`
- 公钥：`D:\work\projects\EliCloud\.deploy-keys\email_deploy_ed25519.pub`

在服务器上追加公钥（幂等，不覆盖已有 key）：

```bash
PUB="$(cat /tmp/email_deploy_ed25519.pub)"   # 先把 .pub 上传到 /tmp
sudo -u deploy bash -c '
  install -d -m 0700 /home/deploy/.ssh
  touch /home/deploy/.ssh/authorized_keys
  grep -qxF "$0" /home/deploy/.ssh/authorized_keys || printf "%s\n" "$0" >> /home/deploy/.ssh/authorized_keys
  chmod 0600 /home/deploy/.ssh/authorized_keys
' "$PUB"
```

本地验证：

```powershell
ssh -i D:\work\projects\EliCloud\.deploy-keys\email_deploy_ed25519 deploy@146.56.237.33 true
```

### 3. 写 `/srv/email/app.env`

```bash
sudo -u deploy bash -c 'umask 077; cat > /srv/email/app.env <<EOF
PUBLIC_BASE_URL=https://146.56.237.33/email
APP_NAME=EliCloud
DATABASE_URL=sqlite:////data/email.db

EMAIL_INTERNAL_TOKEN='"$(openssl rand -hex 32)"'
CALLBACK_SIGNING_SECRET='"$(openssl rand -hex 32)"'
CALLBACK_ALLOWED_HOSTS=sso,main-api,mc,127.0.0.1,localhost

MAIL_MODE=smtp
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_USERNAME=resend
SMTP_STARTTLS=true
SMTP_SSL=false
SMTP_TIMEOUT=10
MAIL_FROM=no-reply@elipese.cloud
MAIL_FROM_NAME=EliCloud

VERIFICATION_TTL_SECONDS=900
MAX_ACTIVE_PER_EMAIL=10
CREATE_COOLDOWN_SECONDS=60
CONFIRM_RATE_LIMIT_PER_MINUTE=10
METADATA_MAX_BYTES=8192
ALLOWED_TEMPLATES=default,register
CALLBACK_MAX_ATTEMPTS=6
CALLBACK_RETRY_SCHEDULE=0,30,120,600,3600,21600
CALLBACK_TIMEOUT_SECONDS=10
CALLBACK_ON_TIMEOUT=true
WORKER_ENABLED=true
WORKER_INTERVAL_SECONDS=5
RETENTION_DAYS=30
TRUST_PROXY_HEADERS=true
LOG_LEVEL=info

EMAIL_IMAGE_REPO=ghcr.io/elicloudorg/email
EMAIL_IMAGE_TAG=prod
EMAIL_GATEWAY_NETWORK=dsh-nas_dsh-net
EOF
chmod 0600 /srv/email/app.env
chown deploy:deploy /srv/email/app.env'
```

**Resend SMTP 密码**不要手打、不要打印，从服务器 Postfix 的 SASL 配置安全追加：

```bash
PASS="$(sudo sed -n 's/^\[smtp.resend.com\]:587[[:space:]]*resend://p' /etc/postfix/sasl_passwd)"
sudo -u deploy sh -c 'umask 077; printf "SMTP_PASSWORD=%s\n" "$1" >> /srv/email/app.env' _ "$PASS"
unset PASS
```

检查（只看变量名，不打印值）：

```bash
sudo -u deploy grep -o '^[A-Z_]*=' /srv/email/app.env | sort
```

### 4. 加 Caddy 路由

在 `/home/docker-admin/dsh-nas/caddy/Caddyfile` 末尾追加（先备份）：

```caddyfile
https://146.56.237.33/email/* {
	tls {
		issuer acme {
			profile shortlived
		}
	}
	# 故意不写 log：confirm 的 token 在 query string 里，避免访问日志泄漏
	route {
		@internal_verify {
			path /email/v1/verify*
			remote_ip 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.1/32
		}
		handle @internal_verify {
			uri strip_prefix /email
			reverse_proxy email:8000 {
				header_up X-Real-IP {remote_host}
			}
		}
		handle /email/v1/confirm* {
			uri strip_prefix /email
			reverse_proxy email:8000 {
				header_up X-Real-IP {remote_host}
			}
		}
		handle /email/v1/healthz {
			uri strip_prefix /email
			reverse_proxy email:8000
		}
		respond 404
	}
}
```

校验后再 reload（**不要跳过 validate**）：

```bash
cd /home/docker-admin/dsh-nas/caddy
cp Caddyfile "Caddyfile.bak.$(date +%Y%m%d-%H%M%S)"
docker run --rm --entrypoint caddy -v /home/docker-admin/dsh-nas/caddy:/etc/caddy:ro \
  caddy:2-alpine validate --config /etc/caddy/Caddyfile
docker exec dsh-caddy caddy reload --config /etc/caddy/Caddyfile
```

如果 validate 失败：恢复刚才的备份，不要 reload。

### 5. 首次发布

1. GitHub → Actions → **Deploy to production** → *Run workflow*，`ref` 填 `prod`；
2. 或者手动 push `prod`（本次初始化**没有**推）：
   ```bash
   git switch prod
   git push -u origin prod
   ```
3. Actions 先构建并推送镜像，再 scp + ssh 执行 `/srv/email/deploy.sh prod`。

> **首次可能卡在 `docker pull`**：ghcr 包默认可能是 private。到 GitHub 组织 → Packages →
> `email` → Package settings → Change visibility → Public（与 `sso` 相同），然后重跑部署；
> 或在 `app.env` 填 `EMAIL_GHCR_USER` / `EMAIL_GHCR_TOKEN`（PAT，需 `read:packages`）。

### 6. 上线验收

```bash
# 公网健康检查
curl -fsS https://146.56.237.33/email/v1/healthz

# 公网 verify 必须 404（Caddy 私网限制）
curl -s -o /dev/null -w '%{http_code}\n' https://146.56.237.33/email/v1/verify

# 无效 token 应得到通用错误页（不是 500）
curl -s -o /dev/null -w '%{http_code}\n' 'https://146.56.237.33/email/v1/confirm?token=bogus'

# 容器与数据
docker inspect email --format '{{.State.Health.Status}}'
sudo ls -l /home/docker-admin/elicloud/email/data
```

内部调用（服务器上，token 从 app.env 读，不打印）：

```bash
TOKEN="$(sudo -u deploy sed -n 's/^EMAIL_INTERNAL_TOKEN=//p' /srv/email/app.env)"
curl -sS -X POST http://127.0.0.1:8010/v1/verify \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","purpose":"generic","callback_url":"http://127.0.0.1:9001/cb"}'
unset TOKEN
```

## GitHub 配置清单（本仓库实际值）

### Secrets

| Secret | 值 |
|---|---|
| `SSH_HOST` | `146.56.237.33` |
| `SSH_USER` | `deploy` |
| `SSH_PORT` | `22` |
| `SSH_KEY` | 部署私钥全文（ed25519；公钥在服务器 `/home/deploy/.ssh/authorized_keys`） |
| `GHCR_TOKEN` | 不需要（同仓库推 ghcr 用内置 `GITHUB_TOKEN`） |

```bash
gh secret set SSH_HOST --body '146.56.237.33'
gh secret set SSH_USER --body 'deploy'
gh secret set SSH_PORT --body '22'
gh secret set SSH_KEY < ./email_deploy_ed25519
```

### Variables（**必须仓库级**）

| Variable | 值 | 说明 |
|---|---|---|
| `DEPLOY_MODE` | `image` | `image` → 阶段一构建并推镜像 |

```bash
gh variable set DEPLOY_MODE --body 'image'
```

### Environment：`production`

- 建议加 Required reviewers；但**单账号无法批准自己触发的部署**，先不挂（挂了自己锁死）。
- 若限制 Deployment branches = `prod`，用 `workflow_dispatch` 回滚时 UI 里的分支必须选 `prod`。

### 分支保护

| 分支 | 规则 |
|---|---|
| `main` | 需要 PR；必过检查 `Test and build`；Require branches to be up to date；禁 force push / 删除 |
| `prod` | 需要 PR；必过检查 `Test and build` + `Guard prod source`；禁 force push / 删除 |

> 单账号不能给自己的 PR 审批，所以 `required_approving_review_count` 先设 0（仍必须走 PR）；
> 加协作者后再调 1（main）/ 2（prod）。
>
> `prod` 的保护要在**首次 push prod 之后**再配；提前配会挡住初始化直推。
> `Guard prod source` 是「prod 只允许从 main 合并」的唯一强制手段，必须加进 prod 必过检查。

## 配置与密钥放置表

| 变量 / 配置 | 位置 | 用途 |
|---|---|---|
| `SSH_HOST` / `SSH_USER` / `SSH_PORT` | GitHub Secrets | Actions 连哪台机器、以谁登录 |
| `SSH_KEY` | GitHub Secrets | 部署私钥（Actions 侧唯一凭据） |
| 对应公钥 | 服务器 `/home/deploy/.ssh/authorized_keys`（600） | 校验上面的私钥 |
| `GITHUB_TOKEN` | Actions 内置 | 同仓库推 ghcr 镜像（靠 `packages: write`） |
| `DEPLOY_MODE` | GitHub Variables（仓库级） | `image` → 阶段一构建推镜像 |
| `PUBLIC_BASE_URL` | `/srv/email/app.env` | 确认链接的唯一来源；当前 `https://146.56.237.33/email` |
| `EMAIL_INTERNAL_TOKEN` | `/srv/email/app.env` | 内部接口 Bearer；空 = 内部接口关闭 |
| `CALLBACK_SIGNING_SECRET` | `/srv/email/app.env` | callback HMAC 密钥 |
| `CALLBACK_ALLOWED_HOSTS` | `/srv/email/app.env` | callback host 白名单（防 SSRF） |
| `SMTP_PASSWORD` | `/srv/email/app.env` | Resend API key；从 Postfix SASL 安全提取，不打印 |
| `EMAIL_IMAGE_REPO` / `EMAIL_IMAGE_TAG` | `/srv/email/app.env`（可选） | 覆盖镜像仓库/tag |
| `EMAIL_GATEWAY_NETWORK` | `/srv/email/app.env`（可选） | 覆盖网关网络 |
| `EMAIL_GHCR_USER` / `EMAIL_GHCR_TOKEN` | `/srv/email/app.env`（可选） | private 镜像包拉取凭据 |
| SQLite `email.db` | `/home/docker-admin/elicloud/email/data`（容器内 `/data`） | 验证请求与 callback outbox |
| 部署 ref | Actions 作为参数传给 `deploy.sh` | 决定这次部署哪个版本 |
| 人工运维私钥 | 本机 `~/.ssh/`（Windows：`C:\Users\<you>\.ssh\...`） | 仅供人登录服务器，与 Actions 无关 |

规则：**部署环节的凭据只走 GitHub Secrets；应用运行时变量只在服务器 `app.env`。**
两边都不要写进仓库，也不要在 workflow 里 `echo` 出来。

## 日常发布流程

```bash
git switch main && git pull
git switch -c feature/xxx
# ... 开发 ...
git push -u origin feature/xxx        # 开 PR → main，等 CI 通过并合并
```

然后发 PR：`main` → `prod`。合并到 `prod` 即自动走 `deploy-prod.yml`：
先构建推镜像，再上传并执行 `/srv/email/deploy.sh prod`。

## 回滚流程

1. **Actions 回滚（推荐）**
   Actions → *Deploy to production* → *Run workflow* → `ref` 填上一个可用版本
   （tag、commit SHA 或分支）。阶段一会按该 ref 检出、构建并把 `:prod` 指回它；
   `sha-<short>` 也是该 commit 的。
   - 若 Environment 限制了 Deployment branches，UI 里的分支必须选 `prod`。
2. **服务器手工回滚**
   ```bash
   sudo -u deploy env EMAIL_IMAGE_TAG=sha-1a2b3c4 /srv/email/deploy.sh prod
   ```
   适合 Actions 不可用时应急；这是绕过审批的路径，操作后补记录。

镜像 tag 保留策略：每次部署推 `prod`（移动）与 `sha-<short>`（不可变，回滚锚点），
建议保留最近 ≥10 个 `sha-` tag。

## 常见问题排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| Actions scp 失败 | `SSH_*` 写错；私钥与服务器公钥不匹配；`/srv/email` 不存在/不可写 | 用 `ssh -i ... deploy@146.56.237.33` 复现；确认目录 `deploy:deploy 750` |
| `deploy.sh: Permission denied` | 文件没有可执行位 | workflow 已 `chmod +x`；手工上传时自己加 |
| `bad interpreter: ...^M` | 上传/提交带了 CRLF | `.gitattributes` 已有 `*.sh text eol=lf`，重新提交 |
| `docker pull` 报 `denied` | 包不存在或 private | 先让阶段一跑一次创建包；把包设为 public 或配 `EMAIL_GHCR_USER/TOKEN` |
| 容器起不来 / unhealthy | app.env 缺必填项、SMTP 配置错、数据目录不可写 | `docker logs email`；`deploy.sh` 失败时会打印最近 50 行 |
| `/email/v1/healthz` 502 | 容器没加入 `dsh-nas_dsh-net`，或 Caddy 块写错 | `docker inspect email` 网络；`caddy validate` 后 reload |
| 确认链接 404 | Caddy 没剥 `/email`，或 `PUBLIC_BASE_URL` 与路由不一致 | 检查 Caddy 块与 `PUBLIC_BASE_URL`；链接应为 `/email/v1/confirm` |
| 邮件进垃圾箱 | SPF/DKIM/Resend 域名信誉 | 已在 Postfix 配 DKIM；检查收件箱垃圾箱；必要时用 Resend 专用域名 |
| SMTP 535 / 认证失败 | `SMTP_PASSWORD` 不是 Resend key 或未追加 | 从 `/etc/postfix/sasl_passwd` 重新安全提取；不要打印 |
| callback 一直 pending | callback host 不在白名单、接收端 4xx/5xx、网络不通 | 看状态接口 `delivery.last_error`；确认 `CALLBACK_ALLOWED_HOSTS` |
| 公网能调 `/email/v1/verify` | Caddy `remote_ip` 没生效 | 检查 `route` 块顺序与 `remote_ip`；同时确认 Bearer 鉴权未关闭 |

## 相关文件与配置位置

| 文件 / 配置 | 位置 | 说明 |
|---|---|---|
| CI 工作流 | `.github/workflows/ci.yml` | PR 到 main/prod、push 到 main |
| 部署工作流 | `.github/workflows/deploy-prod.yml` | push 到 prod、workflow_dispatch |
| 部署脚本（版本管理） | `deploy/deploy.sh` | 上传到 `/srv/email/deploy.sh` 执行；生产编排也在这里生成 |
| 服务器初始化脚本 | `scripts/bootstrap-server.sh` | 在服务器上以 root 跑一次 |
| 部署密钥与服务器信息 | GitHub Secrets | `SSH_HOST`/`SSH_USER`/`SSH_KEY`/`SSH_PORT` |
| 部署模式开关 | GitHub Variables | `DEPLOY_MODE=image` 才构建推镜像 |
| 运行时环境变量 | 服务器 `/srv/email/app.env` | 600，`deploy:deploy`，不进仓库 |
| 本地/手工编排 | 仓库根 `docker-compose.yml` | `build: .` 的本地版本，**不用于生产**；生产编排由 `deploy.sh` 生成 |
