#!/usr/bin/env bash
set -euo pipefail

# 远程部署入口。由 GitHub Actions（.github/workflows/deploy-prod.yml）通过 SSH 调用：
#
#   /srv/email/deploy.sh [ref]
#
# 约定：
#   - 参数：要部署的 Git ref，默认 prod（本脚本只把它打在日志里；镜像是 :prod 或 EMAIL_IMAGE_TAG）
#   - 幂等：可重复执行，重复跑同一 ref 不产生破坏性副作用
#   - 失败必须非 0 退出（set -e 已保证，但请勿自己吞掉错误）
#   - 本文件由仓库 deploy/deploy.sh 同步而来，不要在服务器上直接改；
#     改动请提交到仓库，再由 deploy-prod.yml 上传
#   - 运行时环境变量放在 /srv/email/app.env，不要写进本文件
#
# 本项目的部署形态：
#   - DEPLOY_MODE=image：Actions 构建并推 ghcr.io/elicloudorg/email:prod（+ sha-<short>）
#   - 本脚本拉镜像、生成 compose、重建容器、等 healthy、自检 /v1/healthz
#   - 数据（SQLite）在 /home/docker-admin/elicloud/email/data，容器内 /data
#   - 本机 Docker 是 snap 装的，看不到 /srv；所以只把 /home 下的路径交给 docker，
#     /srv/email/app.env 由 bash 自己读，再写到 /home/deploy/elicloud-email/.env
#
# 回滚：Actions -> Deploy to production -> Run workflow -> ref 填旧版本；
#       或服务器上手工：
#         sudo -u deploy env EMAIL_IMAGE_TAG=sha-1a2b3c4 /srv/email/deploy.sh prod
# ============================

REF="${1:-prod}"
APP_DIR="/srv/email"
APP_ENV="${APP_DIR}/app.env"

PROJECT_NAME="email"
PROJECT_DIR="/home/deploy/elicloud-email"
DATA_PARENT="/home/docker-admin/elicloud/email"
DATA_DIR="${DATA_PARENT}/data"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
HEALTH_TIMEOUT=180

echo "[deploy] ref=${REF} dir=${APP_DIR}"

# ---------- 0) 前置检查 ----------
command -v docker >/dev/null 2>&1 || { echo "[deploy] 找不到 docker" >&2; exit 1; }
[[ -f "${APP_ENV}" ]] || { echo "[deploy] 缺少运行时变量文件 ${APP_ENV}" >&2; exit 1; }

# 从 app.env 安全取值：不做 shell eval，避免值里的特殊字符被当作命令执行
env_get() { sed -n "s/^$1=//p" "${APP_ENV}" | tail -n 1; }

require_env() {
  local name="$1" value
  value="$(env_get "${name}")"
  if [[ -z "${value}" ]]; then
    echo "[deploy] app.env 缺少必填项 ${name}" >&2
    exit 1
  fi
}

for required in PUBLIC_BASE_URL EMAIL_INTERNAL_TOKEN CALLBACK_SIGNING_SECRET CALLBACK_ALLOWED_HOSTS MAIL_FROM; do
  require_env "${required}"
done

MAIL_MODE_VALUE="$(env_get MAIL_MODE)"; MAIL_MODE_VALUE="${MAIL_MODE_VALUE:-smtp}"
if [[ "${MAIL_MODE_VALUE}" == "smtp" ]]; then
  require_env SMTP_HOST
  require_env SMTP_PORT
elif [[ "${MAIL_MODE_VALUE}" == "log" ]]; then
  echo "[deploy] 警告：MAIL_MODE=log，确认链接会写进容器日志；生产建议 smtp" >&2
else
  echo "[deploy] MAIL_MODE 只允许 log 或 smtp（当前 ${MAIL_MODE_VALUE}）" >&2
  exit 1
fi

IMAGE_REPO="$(env_get EMAIL_IMAGE_REPO)";    IMAGE_REPO="${IMAGE_REPO:-ghcr.io/elicloudorg/email}"
IMAGE_TAG="${EMAIL_IMAGE_TAG:-$(env_get EMAIL_IMAGE_TAG)}"; IMAGE_TAG="${IMAGE_TAG:-prod}"
GATEWAY_NETWORK="$(env_get EMAIL_GATEWAY_NETWORK)"; GATEWAY_NETWORK="${GATEWAY_NETWORK:-dsh-nas_dsh-net}"
GHCR_USER="$(env_get EMAIL_GHCR_USER)"
GHCR_TOKEN="$(env_get EMAIL_GHCR_TOKEN)"
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"

docker network inspect "${GATEWAY_NETWORK}" >/dev/null 2>&1 || {
  echo "[deploy] 网关网络不存在：${GATEWAY_NETWORK}" >&2
  exit 1
}

# ---------- 1) 准备 docker 可见的项目目录 ----------
install -d -m 0755 "${PROJECT_DIR}"

# 1a) app.env -> ${PROJECT_DIR}/.env；docker compose 用它做变量插值 + env_file
{
  cat "${APP_ENV}"
  echo ""
  echo "# ---- 以下由 deploy.sh 追加，不在 app.env 里维护 ----"
  echo "EMAIL_IMAGE=${IMAGE}"
  echo "EMAIL_DATA_DIR=${DATA_DIR}"
  echo "EMAIL_GATEWAY_NETWORK=${GATEWAY_NETWORK}"
} > "${PROJECT_DIR}/.env"
chmod 0600 "${PROJECT_DIR}/.env"

# 1b) 生成编排文件（单一真源是仓库；本文件由 deploy.sh 生成，不要手改）
cat > "${COMPOSE_FILE}" <<'YAML'
# 本文件由 /srv/email/deploy.sh 生成；改部署行为请改仓库 deploy/deploy.sh。
name: email

services:
  email:
    image: ${EMAIL_IMAGE}
    container_name: email
    restart: unless-stopped
    # 以宿主机 docker-admin(1002:1003) 运行，数据卷才可写
    user: "1002:1003"
    env_file:
      - .env
    volumes:
      - ${EMAIL_DATA_DIR}:/data
    ports:
      # 只绑回环：本机 curl / SSH 隧道调试用，公网不可达
      - "127.0.0.1:8010:8000"
    networks:
      - gateway
    healthcheck:
      test:
        - "CMD"
        - "python"
        - "-c"
        - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/healthz', timeout=3).status == 200 else 1)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s

networks:
  gateway:
    external: true
    name: ${EMAIL_GATEWAY_NETWORK}
YAML

# ---------- 2) 迁移：清掉属于其它 compose 项目的同名容器 ----------
if docker inspect "${PROJECT_NAME}" >/dev/null 2>&1; then
  existing_project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "${PROJECT_NAME}" 2>/dev/null || true)"
  if [[ "${existing_project}" != "${PROJECT_NAME}" ]]; then
    echo "[deploy] 迁移：现有 ${PROJECT_NAME} 容器属于旧部署（project='${existing_project:-非 compose}'），先移除"
    docker rm -f "${PROJECT_NAME}" >/dev/null
  fi
fi

# ---------- 3) 拉取本次要部署的镜像 ----------
if [[ -n "${GHCR_USER}" && -n "${GHCR_TOKEN}" ]]; then
  echo "[deploy] docker login ${IMAGE_REPO%%/*}（用户 ${GHCR_USER}）"
  printf '%s' "${GHCR_TOKEN}" | docker login "${IMAGE_REPO%%/*}" -u "${GHCR_USER}" --password-stdin >/dev/null
fi

echo "[deploy] docker pull ${IMAGE}"
docker pull "${IMAGE}"

# ---------- 3b) 数据目录：用一次性容器创建/校正（deploy 用户不可遍历 /home/docker-admin） ----------
echo "[deploy] 准备数据目录 ${DATA_DIR}"
docker run --rm --user 0:0 \
  -v "${DATA_PARENT}:/parent" \
  --entrypoint sh "${IMAGE}" -c '
    set -e
    mkdir -p /parent/data
    chown 1002:1003 /parent/data
    chmod 0750 /parent/data
  '
docker run --rm --user 1002:1003 \
  -v "${DATA_DIR}:/data" \
  --entrypoint sh "${IMAGE}" -c 'test -w /data' || {
    echo "[deploy] 数据目录对 uid 1002:1003 不可写：${DATA_DIR}" >&2
    exit 1
  }

# ---------- 4) 重建容器 ----------
docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" up -d

# ---------- 5) 等健康检查通过 ----------
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while :; do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${PROJECT_NAME}" 2>/dev/null || echo missing)"
  case "${status}" in
    healthy)
      echo "[deploy] 容器健康：${status}"
      break
      ;;
    unhealthy)
      echo "[deploy] 容器健康检查失败，最近日志：" >&2
      docker logs --tail 50 "${PROJECT_NAME}" >&2 2>&1 || true
      exit 1
      ;;
    missing|exited|dead)
      echo "[deploy] 容器状态异常：${status}，最近日志：" >&2
      docker logs --tail 50 "${PROJECT_NAME}" >&2 2>&1 || true
      exit 1
      ;;
  esac
  if [[ "$(date +%s)" -ge "${deadline}" ]]; then
    echo "[deploy] 等待健康检查超时（${HEALTH_TIMEOUT}s），当前状态：${status}" >&2
    docker logs --tail 50 "${PROJECT_NAME}" >&2 2>&1 || true
    exit 1
  fi
  sleep 5
done

# ---------- 6) 容器外自检 ----------
if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8010/v1/healthz || true)"
  echo "[deploy] 自检 GET http://127.0.0.1:8010/v1/healthz -> ${code}"
  if [[ "${code}" != "200" ]]; then
    echo "[deploy] 自检失败：期望 200" >&2
    exit 1
  fi
fi

echo "[deploy] 镜像：${IMAGE}"
echo "[deploy] done"
