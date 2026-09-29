#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${CLAUDE_USAGE_DOCKER_IMAGE:-claude-usage}"
APP_CONTAINER="${CLAUDE_USAGE_DOCKER_APP_CONTAINER:-claude-usage}"
PROXY_CONTAINER="${CLAUDE_USAGE_DOCKER_PROXY_CONTAINER:-claude-usage-proxy}"
PRIVATE_NETWORK="${CLAUDE_USAGE_DOCKER_PRIVATE_NETWORK:-claude-usage-private}"
PROXY_NETWORK="${CLAUDE_USAGE_DOCKER_PROXY_NETWORK:-claude-usage-loopback}"
PORT="${CLAUDE_USAGE_DOCKER_PORT:-9898}"
CLAUDE_USAGE_INVOKED_AS=docker
CLAUDE_USAGE_DOCKER_CONTAINER="$APP_CONTAINER"
MANAGED_LABEL="com.claude-usage.managed"
LAUNCH_LABEL="com.claude-usage.launch"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DATA_DIR="${CLAUDE_USAGE_DOCKER_DATA_DIR:-$HOME/.local/share/claude-usage-docker}"
# EXPORTED so `docker run` can be handed the NAME with no `=value`. Passing it
# as `--env CLAUDE_USAGE_API_TOKEN="$API_TOKEN"` put the whole 64-hex bearer
# token in the `docker run` process's argv, and on Linux /proc/<pid>/cmdline is
# mode 444 with no `hidepid` by default -- so any other local user sweeping the
# process table while the launcher runs captures it, and the
# token is the whole of the authentication on a loopback API that answers
# /api/data with every project name, session topic and branch in the database.
# Demonstrated: a `nobody` shell loop caught it; with the name-only form it
# missed over 1,200 sweeps. The value travels in the launcher's OWN environment
# instead, which is /proc/<pid>/environ -- mode 0400, owner-only.
# `vscode-extension/src/server-manager.ts` already passes this same secret by
# environment rather than argv; this was the one surface that did not.
export API_TOKEN CLAUDE_USAGE_API_TOKEN PORT
export CLAUDE_USAGE_INVOKED_AS CLAUDE_USAGE_DOCKER_CONTAINER
API_TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d '[:space:]')"
CLAUDE_USAGE_API_TOKEN="$API_TOKEN"
LAUNCH_ID="$(od -An -N24 -tx1 /dev/urandom | tr -d '[:space:]')"

if [[ ! "$API_TOKEN" =~ ^[0-9a-f]{64}$ ]]; then
  echo "❌  Could not generate a secure dashboard API token." >&2
  exit 1
fi
if [[ ! "$LAUNCH_ID" =~ ^[0-9a-f]{48}$ ]]; then
  echo "❌  Could not generate a Docker launch identity." >&2
  exit 1
fi
APP_NETWORK_ALIAS="app-$LAUNCH_ID"

if [[ -z "$IMAGE" || "$IMAGE" == -* || "$IMAGE" =~ [[:space:]] ]]; then
  echo "❌  Invalid Docker image reference: $IMAGE" >&2
  exit 1
fi

if [[ ! "$PORT" =~ ^[1-9][0-9]{3,4}$ ]] \
  || (( 10#$PORT < 1024 || 10#$PORT > 65535 )); then
  echo "❌  CLAUDE_USAGE_DOCKER_PORT must be a decimal integer from 1024 to 65535 with no leading zero." >&2
  exit 1
fi
# Canonicalize once so Bash, Docker, and Python cannot disagree about the
# numeric value (Bash otherwise treats a leading zero as an octal prefix).
PORT=$((10#$PORT))

for docker_name in "$APP_CONTAINER" "$PROXY_CONTAINER" "$PRIVATE_NETWORK" "$PROXY_NETWORK"; do
  if [[ ! "$docker_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    echo "❌  Invalid Docker container/network name: $docker_name" >&2
    exit 1
  fi
done

if [[ "$CLAUDE_DIR" != /* || "$DATA_DIR" != /* ]]; then
  echo "❌  Claude and database directories must be absolute paths." >&2
  exit 1
fi

# Docker Engine versions before 28 could expose ports published to localhost
# to hosts on the same L2 segment. The isolated gateway mode used below is also
# part of the modern networking contract, so fail closed on older engines.
DOCKER_SERVER_VERSION="$(docker version --format '{{.Server.Version}}')"
DOCKER_SERVER_MAJOR="${DOCKER_SERVER_VERSION%%.*}"
if [[ ! "$DOCKER_SERVER_MAJOR" =~ ^[0-9]+$ || "$DOCKER_SERVER_MAJOR" -lt 28 ]]; then
  echo "❌  Docker Engine 28+ is required for reliable loopback-only publishing (found ${DOCKER_SERVER_VERSION})." >&2
  exit 1
fi

# Linux bind mounts preserve the host UID. Docker Desktop presents protected
# macOS bind mounts as root-owned inside its VM, so use capability-free root
# there solely to traverse those read-only mounts.
if [[ "$(uname -s)" == "Darwin" ]]; then
  CONTAINER_USER="0:0"
else
  CONTAINER_USER="$(id -u):$(id -g)"
fi

if [[ -L "$CLAUDE_DIR" || -L "$CLAUDE_DIR/projects" ]]; then
  echo "❌  Refusing symbolic-link Claude data path: $CLAUDE_DIR/projects" >&2
  exit 1
fi
if [[ ! -d "$CLAUDE_DIR/projects" ]]; then
  echo "❌  Claude projects directory not found: $CLAUDE_DIR/projects" >&2
  exit 1
fi

if [[ "$CLAUDE_DIR/projects" == *","* || "$DATA_DIR" == *","* ]]; then
  echo "❌  Docker bind paths containing commas are not supported." >&2
  exit 1
fi

if [[ -L "$DATA_DIR" || ( -e "$DATA_DIR" && ! -d "$DATA_DIR" ) ]]; then
  echo "❌  Refusing unsafe Docker database directory: $DATA_DIR" >&2
  exit 1
fi
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

# Set MAY_HAVE_CREATED before each Docker create command, closing the signal
# window between Docker creating an object and Bash receiving its ID. Cleanup
# normally uses that immutable ID. If a signal interrupts capture, cleanup
# resolves the mutable name once, verifies the per-launch label, and removes
# only the immutable ID returned by that same inspection.
MAY_HAVE_CREATED_PRIVATE_NETWORK=false
MAY_HAVE_CREATED_PROXY_NETWORK=false
MAY_HAVE_CREATED_APP_CONTAINER=false
MAY_HAVE_CREATED_PROXY_CONTAINER=false
CREATED_PRIVATE_NETWORK_ID=""
CREATED_PROXY_NETWORK_ID=""
CREATED_APP_CONTAINER_ID=""
CREATED_PROXY_CONTAINER_ID=""
PRIVATE_NETWORK_ID=""
PROXY_NETWORK_ID=""

created_container_ref() {
  local id="$1" name="$2" may_have_created="$3" details object_id owner
  if [[ -n "$id" ]]; then
    printf '%s' "$id"
    return
  fi
  [[ "$may_have_created" == true ]] || return 1
  details="$(docker container inspect --format \
    "{{.Id}} {{ index .Config.Labels \"$LAUNCH_LABEL\" }}" \
    "$name" 2>/dev/null)" \
    || return 1
  object_id="${details%% *}"
  owner="${details#* }"
  [[ -n "$object_id" && "$owner" == "$LAUNCH_ID" ]] || return 1
  printf '%s' "$object_id"
}

created_network_ref() {
  local id="$1" name="$2" may_have_created="$3" details object_id owner
  if [[ -n "$id" ]]; then
    printf '%s' "$id"
    return
  fi
  [[ "$may_have_created" == true ]] || return 1
  details="$(docker network inspect --format \
    "{{.Id}} {{ index .Labels \"$LAUNCH_LABEL\" }}" \
    "$name" 2>/dev/null)" \
    || return 1
  object_id="${details%% *}"
  owner="${details#* }"
  [[ -n "$object_id" && "$owner" == "$LAUNCH_ID" ]] || return 1
  printf '%s' "$object_id"
}

cleanup_failed_launch() {
  local status=$? ref
  # Cleanup can block in Docker. Ignore a repeated operator signal while it is
  # in progress so it cannot strand the remaining launch-owned artifacts.
  trap '' INT TERM
  trap - EXIT
  if (( status == 0 )); then
    return
  fi

  set +e
  if [[ "$MAY_HAVE_CREATED_APP_CONTAINER" == true \
    || "$MAY_HAVE_CREATED_PROXY_CONTAINER" == true \
    || "$MAY_HAVE_CREATED_PRIVATE_NETWORK" == true \
    || "$MAY_HAVE_CREATED_PROXY_NETWORK" == true ]]; then
    echo "🧹  Removing Docker artifacts from the failed launch..." >&2
  fi
  if ref="$(created_container_ref "$CREATED_PROXY_CONTAINER_ID" "$PROXY_CONTAINER" "$MAY_HAVE_CREATED_PROXY_CONTAINER")"; then
    docker rm --force "$ref" >/dev/null 2>&1
  fi
  if ref="$(created_container_ref "$CREATED_APP_CONTAINER_ID" "$APP_CONTAINER" "$MAY_HAVE_CREATED_APP_CONTAINER")"; then
    docker rm --force "$ref" >/dev/null 2>&1
  fi
  if ref="$(created_network_ref "$CREATED_PROXY_NETWORK_ID" "$PROXY_NETWORK" "$MAY_HAVE_CREATED_PROXY_NETWORK")"; then
    docker network rm "$ref" >/dev/null 2>&1
  fi
  if ref="$(created_network_ref "$CREATED_PRIVATE_NETWORK_ID" "$PRIVATE_NETWORK" "$MAY_HAVE_CREATED_PRIVATE_NETWORK")"; then
    docker network rm "$ref" >/dev/null 2>&1
  fi
  exit "$status"
}

trap cleanup_failed_launch EXIT
trap 'trap "" INT TERM; exit 130' INT
trap 'trap "" INT TERM; exit 143' TERM

stop_managed_container() {
  local container="$1" details container_id managed
  if ! details="$(docker container inspect --format \
    "{{.Id}} {{ index .Config.Labels \"$MANAGED_LABEL\" }}" \
    "$container" 2>/dev/null)"; then
    return
  fi
  container_id="${details%% *}"
  managed="${details#* }"
  [[ -n "$container_id" ]] || return 0
  if [[ "$managed" != "true" ]]; then
    echo "❌  Refusing to remove unrelated container named ${container}." >&2
    exit 1
  fi
  echo "⏹  Stopping ${container}..."
  docker rm --force "$container_id" >/dev/null
}

echo "▶  Checking for existing containers..."
stop_managed_container "$PROXY_CONTAINER"
stop_managed_container "$APP_CONTAINER"

echo "🔗  Ensuring private application network..."
if PRIVATE_NETWORK_ID="$(docker network inspect --format '{{.Id}}' "$PRIVATE_NETWORK" 2>/dev/null)"; then
  [[ -n "$PRIVATE_NETWORK_ID" ]] || {
    echo "❌  Existing network ${PRIVATE_NETWORK} has no Docker identity." >&2
    exit 1
  }
  PRIVATE_DRIVER="$(docker network inspect --format '{{.Driver}}' "$PRIVATE_NETWORK_ID")"
  PRIVATE_MODE="$(docker network inspect --format '{{index .Options "com.docker.network.bridge.gateway_mode_ipv4"}}' "$PRIVATE_NETWORK_ID")"
  PRIVATE_IPV6="$(docker network inspect --format '{{.EnableIPv6}}' "$PRIVATE_NETWORK_ID")"
  if [[ "$PRIVATE_DRIVER" != "bridge" \
    || "$(docker network inspect --format '{{.Internal}}' "$PRIVATE_NETWORK_ID")" != "true" \
    || "$PRIVATE_MODE" != "isolated" || "$PRIVATE_IPV6" == "true" ]]; then
    echo "❌  Existing network ${PRIVATE_NETWORK} is not fully isolated; remove it before retrying." >&2
    exit 1
  fi
  if [[ "$(docker network inspect --format '{{len .Containers}}' "$PRIVATE_NETWORK_ID")" != "0" ]]; then
    echo "❌  Existing network ${PRIVATE_NETWORK} contains an unrelated container; refusing to share it." >&2
    exit 1
  fi
else
  MAY_HAVE_CREATED_PRIVATE_NETWORK=true
  CREATED_PRIVATE_NETWORK_ID="$(docker network create --internal --ipv6=false --label "$MANAGED_LABEL=true" --label "$LAUNCH_LABEL=$LAUNCH_ID" --opt com.docker.network.bridge.gateway_mode_ipv4=isolated "$PRIVATE_NETWORK")"
  PRIVATE_NETWORK_ID="$CREATED_PRIVATE_NETWORK_ID"
fi

echo "🔗  Ensuring loopback proxy network..."
if PROXY_NETWORK_ID="$(docker network inspect --format '{{.Id}}' "$PROXY_NETWORK" 2>/dev/null)"; then
  [[ -n "$PROXY_NETWORK_ID" ]] || {
    echo "❌  Existing network ${PROXY_NETWORK} has no Docker identity." >&2
    exit 1
  }
  PROXY_DRIVER="$(docker network inspect --format '{{.Driver}}' "$PROXY_NETWORK_ID")"
  HOST_BINDING="$(docker network inspect --format '{{index .Options "com.docker.network.bridge.host_binding_ipv4"}}' "$PROXY_NETWORK_ID")"
  PROXY_IPV6="$(docker network inspect --format '{{.EnableIPv6}}' "$PROXY_NETWORK_ID")"
  if [[ "$PROXY_DRIVER" != "bridge" \
    || "$(docker network inspect --format '{{.Internal}}' "$PROXY_NETWORK_ID")" != "false" \
    || "$HOST_BINDING" != "127.0.0.1" || "$PROXY_IPV6" == "true" ]]; then
    echo "❌  Existing network ${PROXY_NETWORK} is not loopback-only; remove it before retrying." >&2
    exit 1
  fi
  if [[ "$(docker network inspect --format '{{len .Containers}}' "$PROXY_NETWORK_ID")" != "0" ]]; then
    echo "❌  Existing network ${PROXY_NETWORK} contains an unrelated container; refusing to share it." >&2
    exit 1
  fi
else
  MAY_HAVE_CREATED_PROXY_NETWORK=true
  CREATED_PROXY_NETWORK_ID="$(docker network create --ipv6=false --label "$MANAGED_LABEL=true" --label "$LAUNCH_LABEL=$LAUNCH_ID" --opt com.docker.network.bridge.host_binding_ipv4=127.0.0.1 "$PROXY_NETWORK")"
  PROXY_NETWORK_ID="$CREATED_PROXY_NETWORK_ID"
fi

cd "$REPO_DIR"
echo "🔨  Building image..."
docker build -t "$IMAGE" .

echo "🚀  Starting isolated dashboard..."
MAY_HAVE_CREATED_APP_CONTAINER=true
start_app_container() {
  docker run --rm -d \
    --name "$APP_CONTAINER" \
    --network "$PRIVATE_NETWORK_ID" \
    --network-alias "$APP_NETWORK_ALIAS" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 128 \
    --memory 512m \
    --memory-swap 512m \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --user "$CONTAINER_USER" \
    --label "$MANAGED_LABEL=true" \
    --label "$LAUNCH_LABEL=$LAUNCH_ID" \
    --env HOST=0.0.0.0 \
    --env PORT \
    --env CLAUDE_USAGE_INVOKED_AS \
    --env CLAUDE_USAGE_DOCKER_CONTAINER \
    --env CLAUDE_USAGE_ALLOW_CONTAINER_BIND=1 \
    --env CLAUDE_USAGE_API_TOKEN \
    --env CLAUDE_USAGE_SUPPRESS_AUTH_URL=1 \
    --mount "type=bind,src=$CLAUDE_DIR/projects,dst=/home/claudeusage/.claude/projects,readonly" \
    --mount "type=bind,src=$DATA_DIR,dst=/data" \
    "$IMAGE"
}
CREATED_APP_CONTAINER_ID="$(start_app_container)"

echo "🔒  Starting mount-free loopback proxy..."
MAY_HAVE_CREATED_PROXY_CONTAINER=true
create_proxy_container() {
  docker create --rm \
    --name "$PROXY_CONTAINER" \
    --network "$PROXY_NETWORK_ID" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 128 \
    --memory 128m \
    --memory-swap 128m \
    --tmpfs /tmp:rw,noexec,nosuid,size=8m \
    --label "$MANAGED_LABEL=true" \
    --label "$LAUNCH_LABEL=$LAUNCH_ID" \
    --health-cmd "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=2).read()\"" \
    --health-interval 30s \
    --health-timeout 3s \
    --health-retries 3 \
    -p "127.0.0.1:$PORT:$PORT" \
    "$IMAGE" python3 proxy.py \
    --listen-port "$PORT" \
    --target-host "$APP_NETWORK_ALIAS" \
    --target-port "$PORT"
}
CREATED_PROXY_CONTAINER_ID="$(create_proxy_container)"

docker network connect "$PRIVATE_NETWORK_ID" \
  "$CREATED_PROXY_CONTAINER_ID"
docker start "$CREATED_PROXY_CONTAINER_ID" >/dev/null

READY=false
for _ in {1..40}; do
  if docker exec "$CREATED_PROXY_CONTAINER_ID" python3 -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=1).read()" \
      >/dev/null 2>&1; then
    READY=true
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CREATED_PROXY_CONTAINER_ID")" != "true" ]]; then
    break
  fi
  sleep 0.25
done
if [[ "$READY" != "true" ]]; then
  echo "❌  Dashboard containers started but did not become ready." >&2
  echo "    The launcher will remove the containers and networks it created." >&2
  exit 1
fi

echo "✅  Running at http://localhost:${PORT}/#token=${API_TOKEN}"
