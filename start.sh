#!/usr/bin/env bash

set +x
set -Eeuo pipefail
umask 077

REGISTRATION_URL='https://qveris.ai/?ref=afAfj_c90cnWYg'
INVITE_CODE='75gxF1vtvXWj_A'
IMAGE_NAME='qveris-account-proxy:local'
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

lan=false
stop=false
api_key=''
bootstrap_ticket=''
launch_id=''
INITIALIZE_VOLUMES_PY=''
INITIALIZE_ACCOUNTS_PY=''
BOOTSTRAP_TICKET_PY=''

cleanup() {
  local original_status="$1"
  trap - EXIT
  api_key=''
  bootstrap_ticket=''
  launch_id=''
  INITIALIZE_VOLUMES_PY=''
  INITIALIZE_ACCOUNTS_PY=''
  BOOTSTRAP_TICKET_PY=''
  unset api_key bootstrap_ticket launch_id INITIALIZE_VOLUMES_PY INITIALIZE_ACCOUNTS_PY BOOTSTRAP_TICKET_PY || true
  return "$original_status"
}

trap 'cleanup "$?"' EXIT
trap 'exit 130' HUP INT TERM

fail() {
  local detail="${1:-}"
  printf '启动失败。请确认 Docker 正常运行，并检查容器状态。\n' >&2
  if [[ -n "$detail" ]]; then
    printf '原因：%s\n' "$detail" >&2
  fi
  exit 1
}

is_lan_ipv4() {
  local candidate="$1"
  [[ "$candidate" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] &&
    [[ "$candidate" != 0.0.0.0 && "$candidate" != 127.* && "$candidate" != 169.254.* ]]
}

resolve_lan_host() {
  local candidate=''
  if [[ -n "${QVP_LAN_HOST:-}" ]]; then
    printf '%s' "$QVP_LAN_HOST"
    return 0
  fi
  if command -v ip >/dev/null 2>&1; then
    candidate="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true)"
  fi
  if ! is_lan_ipv4 "$candidate" && command -v hostname >/dev/null 2>&1; then
    candidate="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if ! is_lan_ipv4 "$candidate" && command -v ipconfig >/dev/null 2>&1; then
    candidate="$(ipconfig getifaddr en0 2>/dev/null || true)"
  fi
  if is_lan_ipv4 "$candidate"; then
    printf '%s' "$candidate"
  else
    printf 'LAN_IP'
  fi
}

open_url() {
  local url="$1"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1
    return $?
  fi
  if command -v gio >/dev/null 2>&1; then
    gio open "$url" >/dev/null 2>&1
    return $?
  fi
  if command -v wslview >/dev/null 2>&1; then
    wslview "$url" >/dev/null 2>&1
    return $?
  fi
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1
    return $?
  fi
  return 1
}

for argument in "$@"; do
  case "$argument" in
    --lan)
      lan=true
      ;;
    --stop)
      stop=true
      ;;
    --help|-h)
      printf '用法：%s [--lan] [--stop]\n' "$0"
      printf '默认仅本机访问；--lan 监听所有 IPv4 网卡。\n'
      printf '%s\n' '--stop 停止服务并保留 Docker 卷。'
      exit 0
      ;;
    *)
      fail "不支持的启动参数。可用参数：--lan、--stop。"
      ;;
  esac
done

command -v docker >/dev/null 2>&1 || fail "未找到 Docker 命令。"
docker version >/dev/null 2>&1 || fail "Docker Engine 未运行。"
docker compose version >/dev/null 2>&1 || fail "Docker Compose 不可用。"

PROJECT_NAME="${QVP_PROJECT_NAME:-qveris-proxy}"
if [[ ! "$PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
  fail "QVP_PROJECT_NAME 格式无效。"
fi

QVP_HOST_PORT="${QVP_HOST_PORT:-18081}"
if [[ ! "$QVP_HOST_PORT" =~ ^[0-9]+$ ]] || ((QVP_HOST_PORT < 1 || QVP_HOST_PORT > 65535)); then
  fail "QVP_HOST_PORT 必须是 1 到 65535。"
fi

if [[ "$lan" == true ]]; then
  bind_input='0.0.0.0'
else
  bind_input="${QVP_BIND_ADDRESS:-127.0.0.1}"
fi
if [[ "$bind_input" == \[*\] ]]; then
  bind_value="${bind_input:1:${#bind_input}-2}"
else
  bind_value="$bind_input"
fi
if [[ -z "$bind_value" || ! "$bind_value" =~ ^[0-9A-Fa-f:.%]+$ ]]; then
  fail "QVP_BIND_ADDRESS 格式无效。"
fi
if [[ "$bind_value" == *:* ]]; then
  compose_bind="[$bind_value]"
else
  compose_bind="$bind_value"
fi
if [[ "$bind_value" == '0.0.0.0' ]]; then
  browser_host='127.0.0.1'
elif [[ "$bind_value" == '::' ]]; then
  browser_host='[::1]'
elif [[ "$bind_value" == *:* ]]; then
  browser_host="[$bind_value]"
else
  browser_host="$bind_value"
fi

QVP_BIND_ADDRESS="$compose_bind"
QVP_DEFAULT_ACCOUNT="${QVP_DEFAULT_ACCOUNT:-}"
QVP_ROUTING_MODE="${QVP_ROUTING_MODE:-round_robin}"
if [[ "$QVP_ROUTING_MODE" != round_robin && "$QVP_ROUTING_MODE" != explicit ]]; then
  fail "QVP_ROUTING_MODE 必须是 round_robin 或 explicit。"
fi
if [[ -n "${QVP_LAN_HOST:-}" ]] && ! is_lan_ipv4 "$QVP_LAN_HOST"; then
  fail "QVP_LAN_HOST 必须是可供其他局域网设备访问的 IPv4 地址。"
fi
QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES='true'
# These non-secret placeholders satisfy interpolation in the bind-mount overlays.
# compose.quickstart.yaml replaces all three mounts by their container target.
QVP_SECRET_DIR="$SCRIPT_DIR"
QVP_ACCOUNT_SECRETS_DIR="$SCRIPT_DIR"
QVP_CONFIG_DIR="$SCRIPT_DIR"
export QVP_HOST_PORT QVP_BIND_ADDRESS QVP_DEFAULT_ACCOUNT QVP_ROUTING_MODE
export QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES QVP_SECRET_DIR
export QVP_ACCOUNT_SECRETS_DIR QVP_CONFIG_DIR

compose=(
  docker compose
  -p "$PROJECT_NAME"
  -f "$SCRIPT_DIR/compose.yaml"
  -f "$SCRIPT_DIR/compose.lite.yaml"
  -f "$SCRIPT_DIR/compose.ui.yaml"
  -f "$SCRIPT_DIR/compose.quickstart.yaml"
)

if [[ "$stop" == true ]]; then
  printf '正在停止 QVeris Proxy...\n'
  "${compose[@]}" down --remove-orphans || fail "Docker Compose 停止服务失败。"
  printf 'QVeris Proxy 已停止，Docker 卷中的配置和状态会保留。\n'
  exit 0
fi

printf '正在构建 QVeris Proxy 镜像...\n'
"${compose[@]}" build proxy || fail "Docker 镜像构建失败。"

CONFIG_VOLUME="${PROJECT_NAME}_qveris_config"
SECRETS_VOLUME="${PROJECT_NAME}_qveris_secrets"
ACCOUNT_SECRETS_VOLUME="${PROJECT_NAME}_qveris_account_secrets"

ensure_volume() {
  local volume_key="$1"
  local volume_name="$2"
  local actual_labels

  docker volume create \
    --label "com.docker.compose.project=$PROJECT_NAME" \
    --label "com.docker.compose.volume=$volume_key" \
    --label 'io.github.hulwe.qveris.quickstart=1' \
    "$volume_name" >/dev/null || fail "Docker 卷创建失败。"
  actual_labels="$(
    docker volume inspect --format \
      '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}|{{index .Labels "io.github.hulwe.qveris.quickstart"}}' \
      "$volume_name" 2>/dev/null
  )" || fail "Docker 卷归属信息读取失败。"
  if [[ "$actual_labels" != "$PROJECT_NAME|$volume_key|1" ]]; then
    fail "同名 Docker 卷属于其他项目；请设置不同的 QVP_PROJECT_NAME。"
  fi
}

ensure_volume qveris_config "$CONFIG_VOLUME"
ensure_volume qveris_secrets "$SECRETS_VOLUME"
ensure_volume qveris_account_secrets "$ACCOUNT_SECRETS_VOLUME"

IFS= read -r -d '' INITIALIZE_VOLUMES_PY <<'PY' || true
import os
import re
import secrets
import stat

UID = 10001
GID = 10001
ROOTS = ("/config", "/run/secrets", "/run/account-secrets")
STALE = {
    "/config": re.compile(r"\.accounts\.json\.qvp-tmp-[0-9a-f]{32}"),
    "/run/secrets": re.compile(r"\.proxy_access_token\.qvp-tmp-[0-9a-f]{32}"),
}


def require_directory(path):
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise RuntimeError("volume root is not a directory")


def require_regular(path):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise RuntimeError("managed path is not a regular file")


def secure_tree(root):
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        require_directory(current)
        os.chown(current, UID, GID)
        os.chmod(current, 0o700)
        for name in directories:
            require_directory(os.path.join(current, name))
        for name in files:
            path = os.path.join(current, name)
            require_regular(path)
            os.chown(path, UID, GID)
            os.chmod(path, 0o600)


def remove_stale(root, pattern):
    for entry in os.scandir(root):
        if not pattern.fullmatch(entry.name):
            continue
        mode = entry.stat(follow_symlinks=False).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise RuntimeError("stale path has an unexpected type")
        os.unlink(entry.path)


def atomic_create(path, payload):
    directory = os.path.dirname(path)
    name = os.path.basename(path)
    temporary = os.path.join(
        directory, f".{name}.qvp-tmp-{secrets.token_hex(16)}"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchown(descriptor, UID, GID)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


for root in ROOTS:
    require_directory(root)
for root, pattern in STALE.items():
    remove_stale(root, pattern)
for root in ROOTS:
    secure_tree(root)

accounts_path = "/config/accounts.json"
accounts_missing = not os.path.lexists(accounts_path)
if not accounts_missing:
    require_regular(accounts_path)

token_path = "/run/secrets/proxy_access_token"
if not os.path.lexists(token_path):
    token = f"sk-{secrets.token_urlsafe(32)}"
    atomic_create(token_path, token.encode("ascii"))
require_regular(token_path)
os.chown(token_path, UID, GID)
os.chmod(token_path, 0o600)

print("accounts-missing" if accounts_missing else "accounts-present")
PY

mounts=(
  --mount "type=volume,source=$CONFIG_VOLUME,target=/config"
  --mount "type=volume,source=$SECRETS_VOLUME,target=/run/secrets"
  --mount "type=volume,source=$ACCOUNT_SECRETS_VOLUME,target=/run/account-secrets"
)

account_state="$(
  docker run --rm --user 0:0 "${mounts[@]}" \
    --entrypoint python "$IMAGE_NAME" -c "$INITIALIZE_VOLUMES_PY" 2>/dev/null
)" || fail "Docker 卷初始化失败。"
if [[ "$account_state" != 'accounts-missing' && "$account_state" != 'accounts-present' ]]; then
  fail "Docker 卷状态无效。"
fi

if [[ "$account_state" == 'accounts-missing' ]]; then
  printf '首次配置 QVeris Proxy\n'
  printf '注册链接：%s\n' "$REGISTRATION_URL"
  printf '邀请码：%s\n' "$INVITE_CODE"
  if ! open_url "$REGISTRATION_URL"; then
    printf '浏览器未自动打开，请手动访问上面的注册链接。\n'
  fi

  if [[ ! -t 0 ]]; then
    fail "首次配置需要在交互式终端中运行。"
  fi
  while :; do
    printf '粘贴 QVeris API Key（输入不会显示）：' >&2
    if ! IFS= read -r -s api_key; then
      printf '\n' >&2
      fail "API Key 输入已中断。"
    fi
    printf '\n' >&2
    if [[ ${#api_key} -ge 8 && ${#api_key} -le 4096 && "$api_key" =~ ^[A-Za-z0-9._-]+$ ]]; then
      break
    fi
    api_key=''
    printf 'API Key 格式无效，请重新输入。\n' >&2
  done

  IFS= read -r -d '' INITIALIZE_ACCOUNTS_PY <<'PY' || true
import json
import os
import re
import secrets
import stat
import sys

UID = 10001
GID = 10001
path = "/config/accounts.json"

if os.path.lexists(path):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise RuntimeError("accounts path is not a regular file")
    raise SystemExit(0)

raw = sys.stdin.buffer.read(4097).rstrip(b"\r\n")
try:
    api_key = raw.decode("ascii")
except UnicodeDecodeError as error:
    raise RuntimeError("invalid API key") from error
if not re.fullmatch(r"[A-Za-z0-9._-]{8,4096}", api_key):
    raise RuntimeError("invalid API key")

profile_id = secrets.token_hex(16)
accept_language = secrets.choice(
    (
        "zh-CN,zh;q=0.9,en;q=0.8",
        "zh-CN,zh;q=0.9",
        "en-US,en;q=0.9,zh-CN;q=0.8",
    )
)
document = {
    "accounts": [
        {
            "id": "account-a",
            "name": "账号 1",
            "weight": 1,
            "requests_per_minute": 10,
            "burst": 10,
            "transport": {
                "user_agent": f"qveris-account-proxy/0.1.0 profile/{profile_id}",
                "accept_language": accept_language,
            },
            "keys": [{"id": "primary", "api_key": api_key}],
            "oauth_tokens": [],
        }
    ]
}
payload = (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
api_key = ""
raw = b""

directory = os.path.dirname(path)
temporary = os.path.join(
    directory, f".accounts.json.qvp-tmp-{secrets.token_hex(16)}"
)
descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    os.fchown(descriptor, UID, GID)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb", closefd=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError:
        pass
finally:
    os.close(descriptor)
    if os.path.lexists(temporary):
        os.unlink(temporary)

if not stat.S_ISREG(os.lstat(path).st_mode):
    raise RuntimeError("accounts path is not a regular file")
os.chown(path, UID, GID)
os.chmod(path, 0o600)
PY

  if ! printf '%s' "$api_key" | docker run --rm -i --user 0:0 \
    --mount "type=volume,source=$CONFIG_VOLUME,target=/config" \
    --entrypoint python "$IMAGE_NAME" -c "$INITIALIZE_ACCOUNTS_PY" \
    >/dev/null 2>&1; then
    api_key=''
    fail "首个账号写入失败。"
  fi
  api_key=''
  unset api_key
fi

docker run --rm --user 10001:10001 "${mounts[@]}" \
  --entrypoint sh "$IMAGE_NAME" -c \
  'test -r /config/accounts.json && test -w /config && test -r /run/secrets/proxy_access_token && test -x /run/account-secrets' \
  >/dev/null || fail "Docker 卷权限或文件状态无效。"

printf '正在启动轻量可视化服务...\n'
"${compose[@]}" up -d proxy || fail "Docker Compose 启动服务失败。"

container_id="$("${compose[@]}" ps -q proxy)" || fail "代理容器查询失败。"
if [[ -z "$container_id" ]]; then
  fail "代理容器未创建。"
fi

printf '正在等待服务就绪...\n'
healthy=false
for ((attempt = 0; attempt < 60; attempt++)); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
  if [[ "$health" == healthy ]]; then
    healthy=true
    break
  fi
  if [[ "$health" == exited || "$health" == dead ]]; then
    break
  fi
  sleep 2
done
if [[ "$healthy" != true ]]; then
  fail "代理服务未在规定时间内就绪；请运行 docker compose ps 查看状态。"
fi

admin_url="http://${browser_host}:${QVP_HOST_PORT}/admin/"
if [[ "$lan" == true ]]; then
  api_host="$(resolve_lan_host)"
else
  api_host="$browser_host"
fi
base_url="http://${api_host}:${QVP_HOST_PORT}/api/v1"
IFS= read -r -d '' BOOTSTRAP_TICKET_PY <<'PY' || true
import json
import re
import urllib.request

with open("/run/secrets/proxy_access_token", "r", encoding="ascii") as stream:
    token = stream.read().strip()
request = urllib.request.Request(
    "http://127.0.0.1:8080/admin/v1/bootstrap-ticket",
    data=b"",
    method="POST",
    headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    },
)
token = ""
with urllib.request.urlopen(request, timeout=3) as response:
    ticket = json.load(response).get("ticket", "")
if not isinstance(ticket, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket) is None:
    raise RuntimeError("invalid bootstrap ticket")
print(ticket)
PY

bootstrap_ticket="$(
  docker exec --user 10001:10001 "$container_id" \
    python -c "$BOOTSTRAP_TICKET_PY" 2>/dev/null
)" || bootstrap_ticket=''
launch_id="$(date +%s)-$$"
if [[ "$bootstrap_ticket" =~ ^[A-Za-z0-9_-]{43}$ ]]; then
  auto_connect=true
  launch_url="${admin_url}?launch=${launch_id}#bootstrap_ticket=${bootstrap_ticket}"
else
  auto_connect=false
  launch_url="${admin_url}?launch=${launch_id}"
fi
if open_url "$launch_url"; then
  browser_opened=true
else
  browser_opened=false
fi
bootstrap_ticket=''
launch_id=''
launch_url=''

printf 'QVeris Proxy 已就绪。\n'
printf '管理页：%s\n' "$admin_url"
printf 'API Base URL：%s\n' "$base_url"
if [[ "$lan" == true && "$api_host" == LAN_IP ]]; then
  printf '未自动识别局域网地址：请把 LAN_IP 换成这台电脑的 IPv4 地址，或设置 QVP_LAN_HOST 后重启。\n'
fi
if [[ "$auto_connect" == true ]]; then
  printf '管理页已自动连接；可在“运行状态”的“接入应用”区域显示或复制代理 API Key。\n'
else
  printf '自动连接链接生成失败。请运行下面的命令显示管理登录令牌，再在管理页展开“手动连接”：\n'
  printf 'docker run --rm --user 10001:10001 --mount type=volume,source=%s,target=/run/secrets --entrypoint cat %s /run/secrets/proxy_access_token\n' \
    "$SECRETS_VOLUME" "$IMAGE_NAME"
fi
if [[ "$browser_opened" != true ]]; then
  printf '浏览器未自动打开，请手动访问管理页；重新运行启动脚本会再次尝试自动连接。\n'
fi
