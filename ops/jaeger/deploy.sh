#!/usr/bin/env bash
# 一键部署 Jaeger(正式服务)到远程服务器 —— all-in-one(badger 持久化) + Caddy(basic auth)
#
# 用法(在本机执行, 需已配置 ssh 免密到服务器):
#   ./deploy.sh <服务器host> <用户名> <密码>
#   例: ./deploy.sh 47.98.234.129 admin mypass
#
# 部署后:
#   - UI:     http://<host>:16686   (basic auth: 用户名/密码)
#   - OTLP:   http://<host>:4318    (basic auth, 上报需带 Authorization: Basic <base64(用户:密码)>)
#   - 数据:   服务器 /data/jaeger(badger 文件, 重启不丢)
#
# 镜像源: 国内网络用 DaoCloud(docker.m.daocloud.io), 阿里云加速器对 jaeger 不代理(实测 404)
set -euo pipefail

HOST="${1:?用法: deploy.sh <host> <user> <pass>}"
AUTH_USER="${2:?}"
AUTH_PASS="${3:?}"
MIRROR="docker.m.daocloud.io"
JAEGER_TAG="2.5.0"

echo "==> 1/4 拉取镜像(服务器上)"
ssh -o BatchMode=yes -o ConnectTimeout=10 "root@${HOST}" "
  docker pull ${MIRROR}/jaegertracing/jaeger:${JAEGER_TAG} >/dev/null 2>&1 || docker pull ${MIRROR}/jaegertracing/jaeger:${JAEGER_TAG}
  docker pull ${MIRROR}/library/caddy:2 >/dev/null 2>&1
  docker network create jaeger-net >/dev/null 2>&1 || true
"

echo "==> 2/4 启动 Jaeger(badger 持久化, 仅容器内端口)"
ssh -o BatchMode=yes -o ConnectTimeout=10 "root@${HOST}" "
  docker rm -f jaeger >/dev/null 2>&1 || true
  docker run -d --name jaeger --network jaeger-net --restart=unless-stopped \
    -e SPAN_STORAGE_TYPE=badger -e BADGER_EPHEMERAL=false \
    -e BADGER_DIRECTORY_KEY=/badger -e BADGER_DIRECTORY_VALUE=/badger \
    -v /data/jaeger:/badger ${MIRROR}/jaegertracing/jaeger:${JAEGER_TAG}
"

echo "==> 3/4 生成 basic auth hash 并配置 Caddy"
ssh -o BatchMode=yes -o ConnectTimeout=10 "root@${HOST}" "
  HASH=\$(docker run --rm ${MIRROR}/library/caddy:2 caddy hash-password --plaintext '${AUTH_PASS}' 2>/dev/null | tail -1)
  cat > /root/Caddyfile << CADDYEOF
{
	admin off
}
:16686 {
	basic_auth {
		${AUTH_USER} \${HASH}
	}
	reverse_proxy jaeger:16686
}
:4318 {
	basic_auth {
		${AUTH_USER} \${HASH}
	}
	reverse_proxy jaeger:4318
}
CADDYEOF
  docker rm -f caddy >/dev/null 2>&1 || true
  docker run -d --name caddy --network jaeger-net --restart=unless-stopped \
    -p 0.0.0.0:16686:16686 -p 0.0.0.0:4318:4318 \
    -v /root/Caddyfile:/etc/caddy/Caddyfile:ro ${MIRROR}/library/caddy:2
"

echo "==> 4/4 验证"
sleep 4
ssh -o BatchMode=yes -o ConnectTimeout=10 "root@${HOST}" "docker ps --format '{{.Names}} | {{.Status}}' | grep -E 'jaeger|caddy'"
UNAUTH=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 8 "http://${HOST}:16686/" || echo timeout)
echo "无认证访问 UI: HTTP ${UNAUTH}(应为 401 = 认证生效)"
echo ""
echo "✅ 部署完成:"
echo "  UI 访问:   http://${HOST}:16686  (${AUTH_USER} / ${AUTH_PASS})"
echo "  OTLP 上报: http://${HOST}:4318"
echo "  上报鉴权:  OTEL_EXPORTER_OTLP_HEADERS=\"Authorization=Basic \$(printf '${AUTH_USER}:${AUTH_PASS}' | base64)\""
echo "  数据目录:  服务器 /data/jaeger(迁移用 migrate.sh)"
