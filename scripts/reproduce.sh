#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MINIVECTOR_IMAGE:-minivectordb:team}"
action="${1:-check}"
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo '需要可用的 Docker daemon；本机无需安装 Python、NumPy 或 GCC。' >&2
  exit 1
fi
build_image() {
  local -a options=(--platform linux/amd64 -t "$image")
  if [[ -n "${MINIVECTOR_PIP_INDEX_URL:-}" ]]; then
    options+=(--build-arg "PIP_INDEX_URL=$MINIVECTOR_PIP_INDEX_URL")
  fi
  docker build "${options[@]}" "$repo_root"
}
ensure_image() {
  if [[ "${MINIVECTOR_SKIP_BUILD:-0}" == 1 ]]; then
    docker image inspect "$image" >/dev/null
  else
    build_image
  fi
}
case "$action" in
  build) build_image ;;
  check|unit)
    ensure_image
    mkdir -p "$repo_root/artifacts/team"
    if [[ "$action" == check ]]; then
      docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 128m --memory-swap 128m \
        --mount "type=bind,src=$repo_root/scripts,dst=/checks,readonly" \
        --mount "type=bind,src=$repo_root/mysqltest,dst=/cases,readonly" \
        --mount "type=bind,src=$repo_root/artifacts/team,dst=/reports" \
        --entrypoint python3 "$image" /checks/check_public.py --cases /cases --report /reports/public-report.json
    else
      docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 128m --memory-swap 128m \
        --mount "type=bind,src=$repo_root/tests,dst=/app/tests,readonly" \
        --entrypoint python3 "$image" -m unittest discover -s tests -q
    fi ;;
  run)
    ensure_image
    exec docker run --rm -i --platform linux/amd64 --network none \
      --mount "type=volume,src=${MINIVECTOR_VOLUME:-minivectordb-team-data},dst=/data" "$image" ;;
  export)
    build_image
    archive="${2:-$repo_root/artifacts/minivectordb-image.tar.gz}"
    if [[ -e "$archive" ]]; then echo "文件已存在：$archive" >&2; exit 1; fi
    mkdir -p "$(dirname -- "$archive")"
    archive_tmp="$(mktemp "${archive}.tmp.XXXXXX")"
    trap 'rm -f -- "$archive_tmp"' EXIT
    docker save "$image" | gzip > "$archive_tmp"
    mv -- "$archive_tmp" "$archive"
    echo "已导出：$archive" ;;
  load)
    if [[ -z "${2:-}" ]]; then echo '用法：bash scripts/reproduce.sh load 镜像.tar.gz' >&2; exit 1; fi
    gzip -dc -- "$2" | docker load ;;
  *) echo '用法：bash scripts/reproduce.sh {check|unit|build|run|export [文件]|load 文件}' >&2; exit 1 ;;
esac
