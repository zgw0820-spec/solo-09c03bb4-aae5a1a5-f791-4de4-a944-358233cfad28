# image-digest-admission

Kubernetes mutating admission webhook (FastAPI) that rewrites mutable image
**tags** into OCI **index digests** on Deployment create/update.

## 为什么需要它

镜像标签是可变的：标签被重新指向另一个索引后，同一轮滚动发布中先拉起和
后拉起的副本可能拉到不同版本。本 webhook 在准入阶段把 Pod 模板里的标签
引用一次性替换成索引摘要（`repo@sha256:...`），让一次发布里所有副本都锚定
同一份多架构索引，标签之后如何改指都不影响本次发布。

## 行为约定

- 只接收 `admission.k8s.io/v1` 的 `AdmissionReview`，只处理
  `apps/deployments` 的 `CREATE` / `UPDATE`；其他请求直接放行。
- 只处理本地仓库（默认 `localhost:5000`、`registry:5000`，由
  `ALLOWED_REGISTRIES` 配置）。引用其他 registry 的镜像直接拒绝。
- 容器与初始化容器中**带标签**的镜像被替换为 OCI 索引摘要；已经带
  `@sha256:` 摘要的引用保持原样，且不会触发任何仓库请求。
- 同一个 `(registry, repository, tag)` 在一次准入调用中只解析一次
  （请求内缓存）；缓存不跨请求，下一次带标签提交会重新查询仓库，因此
  标签改指会在新的一次准入中被观察到。
- 平台选择：
  - Pod 模板 `nodeSelector` 含 `kubernetes.io/arch` 时，只核对
    `linux/<该架构>`；
  - 否则同时核对 `linux/amd64` 与 `linux/arm64`。
- 所有要求的平台必须能在**同一个索引**中找到描述符，并且每个子清单都能
  按索引里记录的摘要从仓库取到（摘要不一致也算失败）。
- 任一镜像核对失败：拒绝整次请求，响应中给出镜像和中文原因，不返回补丁。

## 目录结构

```
app/
  main.py                 FastAPI 入口：POST /mutate、GET /healthz
  admission/
    config.py             环境变量配置
    images.py             镜像引用解析
    registry.py           OCI Distribution API 客户端 + 平台核对
    policy.py             架构选择、请求内缓存、JSON Patch 生成
scripts/
  seed_registry.py        纯标准库：向本地仓库推送 OCI 测试夹具
  send_admission.py       纯标准库：发送 AdmissionReview 并解码补丁
samples/                  6 个可直接提交的 AdmissionReview 请求
tests/                    34 个离线测试（unittest，仅依赖标准库）
docker-compose.yml        webhook + 本地 registry
Dockerfile                webhook 镜像
```

## 用 Docker Compose 启动

```bash
docker compose up -d --build

# 1. 推送测试夹具（多架构索引 / 缺平台索引 / 子清单被删除的索引）
python3 scripts/seed_registry.py seed

# 2. 健康检查
curl -s http://localhost:8080/healthz
```

### 场景一：正常改写，标签改指后重新解析

```bash
# 首次创建：demo:v1 被改写为索引摘要（init 容器和业务容器同一标签只查一次）
python3 scripts/send_admission.py samples/deployment-create.json
# allowed=true，patch 中两条 replace 均指向同一个 sha256 索引摘要

# 模拟标签改指：把 demo:v1 重新指向 v2 的索引
python3 scripts/seed_registry.py retag-demo

# 再次提交同样的标签引用：新一次准入重新查仓库，补丁里的摘要随之变化
python3 scripts/send_admission.py samples/deployment-update.json

# 恢复现场
python3 scripts/seed_registry.py reset-demo
```

### 场景二：缺少所需子清单（两种失败都给镜像和原因，不带补丁）

```bash
# 2a. 索引里只有 arm64，默认要同时核对 amd64+arm64 -> 拒绝
python3 scripts/send_admission.py samples/deployment-missing-platform.json
# allowed=false: ...索引...中缺少平台 linux/amd64 的子清单描述符

# 同样的镜像加上 kubernetes.io/arch=arm64 选择器 -> 只查 arm64，放行
python3 scripts/send_admission.py samples/deployment-arch-selector.json

# 2b. 索引声明了两个平台，但 amd64 子清单已按摘要删除（404）-> 拒绝
python3 scripts/send_admission.py samples/deployment-broken-child.json
# allowed=false: 镜像 'localhost:5000/apps/broken:v1' 核对失败: 清单不存在...

# 非本地仓库 -> 拒绝
python3 scripts/send_admission.py samples/deployment-non-local-registry.json
```

### 响应示例

成功（补丁为 base64 编码的 JSONPatch）：

```json
{
  "apiVersion": "admission.k8s.io/v1",
  "kind": "AdmissionReview",
  "response": {
    "allowed": true,
    "uid": "req-create-demo",
    "patchType": "JSONPatch",
    "patch": "W3sib3AiOiJyZXBsYWNlIiwicGF0aCI6Ii9zcGVjL3RlbXBsYXRlL3NwZWMvaW5pdENvbnRhaW5lcnMvMC9pbWFnZSIsInZhbHVlIjoibG9jYWxob3N0OjUwMDAvYXBwcy9kZW1vQHNoYTI1NjouLi4ifV0="
  }
}
```

拒绝：

```json
{
  "apiVersion": "admission.k8s.io/v1",
  "kind": "AdmissionReview",
  "response": {
    "allowed": false,
    "uid": "req-missing-platform",
    "status": {"code": 403, "message": "镜像 'localhost:5000/apps/arm-only:v1' 核对失败: 索引 sha256:... 中缺少平台 linux/amd64 的子清单描述符"}
  }
}
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ALLOWED_REGISTRIES` | `localhost:5000,registry:5000` | 允许解析的本地 registry host 列表，逗号分隔 |
| `REGISTRY_UPSTREAM` | `http://registry:5000` | webhook 访问仓库用的地址（可与 workload 里的 host 不同） |
| `REGISTRY_TIMEOUT` | `5` | 单次仓库 HTTP 请求超时（秒） |

注意：补丁里保留镜像原始 host（如 `localhost:5000`），只把标签换成
`@sha256:...`；webhook 自身通过 `REGISTRY_UPSTREAM` 访问仓库。

## 测试

测试不需要 Docker 或第三方库，只用 Python 标准库：

```bash
python3 -I tests/run.py
# 34 tests OK：引用解析、平台选择、索引解析、请求内只解析一次、
# 标签改指重新查询、缺平台/缺子清单/摘要不一致拒绝、真实 HTTP 端到端
```

`tests/test_http_e2e.py` 会在本机临时端口起一个实现了 Distribution v2
清单子集的 HTTP 服务，走完整的 URL/请求头/摘要头处理路径。

## 尚未完成 / 生产化差距

- **TLS 与鉴权**：当前为方便 Compose 演示使用明文 HTTP。Kubernetes 的
  `ValidatingWebhookConfiguration` / `MutatingWebhookConfiguration` 要求
  HTTPS（通常配合 cert-manager 注入 CA Bundle），部署清单与证书签发未包含。
- **没有内置 Registry 认证**：仅支持匿名本地仓库；生产环境需要 token /
  basic auth 与凭据管理。
- 架构选择器只识别 `nodeSelector` 中的 `kubernetes.io/arch`；
  `nodeAffinity`、多架构 `In` 取值、`OS/version/variant` 维度未处理。
- 不接受嵌套索引（index 指向 index）；只核对清单可达性，未核对 blob 层。
- 仓库查询失败目前快速失败拒绝请求，没有重试；高并发下的限流/熔断未实现。
- Compose 面向演示；未提供在真实集群安装 webhook 所需的
  Service / MutatingWebhookConfiguration 清单。
