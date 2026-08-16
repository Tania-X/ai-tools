# Jaeger 可观测性平台(正式服务)运维手册

> 2026-08-16 · 生产化第 ② 项(OTel)的落地基础设施
> 形态: 远程服务器 Docker 部署, all-in-one(badger 持久化) + Caddy(basic auth)

## 架构

```
GitHub Action / 本机审查(demo_otel_trace.py)
    │  OTLP POST /v1/traces + Authorization: Basic
    ▼
Caddy(0.0.0.0:4318, basic auth) ──▶ Jaeger(容器内 4318, badger 存储)
浏览器 UI ──▶ Caddy(0.0.0.0:16686, basic auth) ──▶ Jaeger(容器内 16686)
```

- Jaeger 与 Caddy 同在 docker 网络 `jaeger-net`, Jaeger 不暴露主机端口(仅容器内), 全部公网入口经 Caddy 认证
- 数据: `/data/jaeger`(badger 文件), 容器 `--restart=unless-stopped` 自动拉起

## 一键部署

```bash
./deploy.sh <服务器host> <用户名> <密码>
# 例: ./deploy.sh 47.98.234.129 admin mypass
```
部署后: UI `http://<host>:16686`, OTLP `http://<host>:4318`, 均需 basic auth。
> 别忘了云平台安全组放行 16686 / 4318。

## 上报方式(审查链路写可观测性数据)

**ai-review(GitHub Action)**: action 支持 `otel-enabled / otel-endpoint / otel-service-name / otel-headers` 四个 inputs,
workflow 里配好即每次审查自动留痕:

```yaml
- uses: Tania-X/ai-tools/pr-review@main
  with:
    api-key: ${{ secrets.DEEPSEEK_API_KEY }}
    otel-enabled: "1"
    otel-endpoint: "http://<host>:4318"
    otel-service-name: "ai-tools-review"
    otel-headers: ${{ secrets.JAEGER_OTLP_AUTH }}   # = Authorization=Basic <base64(user:pass)>
```

**本机 demo / 脚本**:
```bash
OTEL_ENABLED=1 OTEL_SERVICE_NAME=ai-tools-review \
OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:4318 \
"OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic $(printf 'user:pass' | base64)" \
python l3-eval/demo_otel_trace.py
```

## 数据迁移(服务到期 / 换服务器)

```bash
./migrate.sh backup  <host>            # 打包到本机 jaeger-data.tar.gz
./migrate.sh restore <host> [tar文件]   # 上传并恢复(旧数据留 /data/jaeger.bak)
```
badger 为文件存储, 同版本 Jaeger 直接识别, 迁移零丢失。

## 常见问题

| 问题 | 处理 |
|------|------|
| 上报 404 | OTLP endpoint 需带 `/v1/traces` 路径或裸端口(新版 SDK 不自动补, 我们的 otel.py 已兼容裸端口) |
| 镜像拉不到 | 阿里云加速器对 jaeger 404, 用 DaoCloud: `docker.m.daocloud.io/jaegertracing/jaeger` |
| 无认证也能访问 | 检查 Caddy 容器是否在跑 / basic_auth 配置 |
| 端口冲突 | Jaeger 只绑容器内, 公网端口全归 Caddy(docker 网络方案) |
| 换密码 | 重新跑 deploy.sh(会重建 Caddy 与 hash) |

## 安全提醒

- Jaeger UI 无内建认证, 依赖 Caddy basic auth —— **不要移除 Caddy 或开放裸端口**
- 建议安全组 16686 仅对需要查看的 IP 开放; 4318 对上报来源开放
- 凭证勿提交到仓库(migrate/deploy 均为参数传入)
