#!/usr/bin/env bash
# Jaeger 数据迁移(badger 文件) — 服务到期/换服务器时转移数据, 零丢失
#
# 用法(在本机执行, 需 ssh 免密):
#   ./migrate.sh backup  <服务器host>            # 打包服务器 /data/jaeger 到本地 jaeger-data.tar.gz
#   ./migrate.sh restore <服务器host> <tar文件>   # 上传本地 tar 到服务器并恢复(需先部署好 Jaeger)
#
# 说明:
#   - badger 是文件型存储, 同版本 Jaeger 直接识别, 打包即搬走
#   - restore 前建议先停 Jaeger: docker stop jaeger(避免文件被写)
set -euo pipefail

CMD="${1:?用法: migrate.sh backup|restore <host> [tar]}"
HOST="${2:?}"
TAR="jaeger-data.tar.gz"

if [ "$CMD" = "backup" ]; then
  echo "==> 停 Jaeger + 打包 /data/jaeger"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "root@${HOST}" \
    "docker stop jaeger >/dev/null 2>&1 || true; tar czf /root/${TAR} -C /data jaeger; docker start jaeger >/dev/null 2>&1 || true; ls -lh /root/${TAR}"
  echo "==> 下载到本机 ${TAR}"
  scp -o BatchMode=yes "root@${HOST}:/root/${TAR}" "./${TAR}"
  echo "✅ 备份完成: ./${TAR}"

elif [ "$CMD" = "restore" ]; then
  LOCAL="${3:-$TAR}"
  [ -f "$LOCAL" ] || { echo "本地文件不存在: $LOCAL"; exit 1; }
  echo "==> 上传 ${LOCAL} 到服务器"
  scp -o BatchMode=yes "./${LOCAL}" "root@${HOST}:/root/${TAR}"
  echo "==> 停 Jaeger + 恢复数据 + 启动"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "root@${HOST}" \
    "docker stop jaeger >/dev/null 2>&1 || true; rm -rf /data/jaeger.bak; [ -d /data/jaeger ] && mv /data/jaeger /data/jaeger.bak; mkdir -p /data && tar xzf /root/${TAR} -C /data; docker start jaeger; sleep 3; docker ps --filter name=jaeger --format '{{.Names}} | {{.Status}}'"
  echo "✅ 恢复完成(旧数据保留在 /data/jaeger.bak, 确认无误后可删)"

else
  echo "未知命令: $CMD(支持 backup / restore)"; exit 1
fi
