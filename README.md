# image-digest-admission

两个协同能力，共用同一个本地 OCI 仓库与 FastAPI 进程：

1. **Mutating admission webhook**：Deployment create/update 时把可变镜像
   **标签**改写成 OCI **索引摘要**（`/mutate`，行为保持不变）。
2. **多架构合版 API（合版）**：linux/amd64 与 linux/arm64 两路 CI 各自构建并
   推送子清单到同一本地仓库后，按构建批次登记回报、核对平台，并在两路有效
   回报收齐时写入一份 OCI 多架构索引，返回可直接拉取的摘要引用。

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
  main.py                 FastAPI 入口：POST /mutate、合版 API、GET /healthz
  admission/              准入 webhook（引用解析、平台核对、JSON Patch）
  merger/                 合版功能（纯标准库，可离线单测）
    models.py             入参校验、平台/回报领域模型
    store.py              单进程内线程安全状态：尝试号优胜、冲突规则
    registry_gateway.py   OCI Distribution 客户端：拉子清单/配置 blob、推索引
    service.py            登记校验与发布编排（锁拆分、冲突回退）
scripts/
  seed_registry.py        纯标准库：向本地仓库推送 OCI 准入测试夹具
  send_admission.py       纯标准库：发送 AdmissionReview 并解码补丁
  ci_build_report.py      模拟单平台 CI：构建推送子清单 + 发送回报
  publish_batch.py        触发合版 / 查询批次状态
samples/
  deployment-*.json       6 个 AdmissionReview 请求
  merge/                  合版 API 的 curl 样例请求
tests/                    68 个离线测试（unittest，仅依赖标准库）
docker-compose.yml        webhook + 本地 registry
Dockerfile                webhook 镜像
```

## 合版 API（多架构批次合并）

### 数据流与规则

1. 两路 CI（`linux/amd64`、`linux/arm64`）各自构建镜像，把子清单（child
   manifest）按摘要推到**同一本地仓库**。
2. 每路 CI 调用 `POST /api/v1/reports` 回报：仓库路径、构建批次、平台、
   尝试号（attempt，≥1）、子清单摘要。
3. 服务端**登记前**用回报摘要拉取子清单，再拉它的 **config blob**，核对其中
   的 `os` / `architecture` 与回报平台一致；核对不过（含子清单或配置 blob
   不存在、媒体类型不对、平台不符）直接 422，回报不入库。
4. 同批次同平台只保留**尝试号最大的有效回报**：
   - 更大尝试号覆盖旧值；更旧的迟到回报返回 `superseded` 且不改状态；
   - 同尝试号同摘要返回 `duplicate`；
   - **同尝试号不同摘要返回 409 冲突**，已登记内容保持不变。
5. 发布员调用 `POST /api/v1/publish` 指定仓库路径与批次：两路有效回报都
   收齐后，按回报摘要构建并写入 OCI 索引（不改任何标签），返回
   `localhost:5000/<repo>@sha256:<index>` 形式的可拉取引用。
6. 写索引期间照常接收回报（registry I/O 不持状态锁）。提交发布结果前再做
   一次原子校验：若本批任一路有更高尝试号回报（或基准摘要变化）在登记后
   生效，本次发布返回 **409 冲突**、已写索引不作为发布结果。
7. 发布失败或冲突时，响应里返回**上次成功的引用** `last_good_reference`
   （首次发布尚无成功记录时为 `null`）；按最新回报重试即可重新合版。
8. 状态只保存在单进程内存中，重启即清空；不创建、不修改、不删除任何标签。

### 接口一览

| 方法与路径 | 说明 | 主要状态码 |
| --- | --- | --- |
| `POST /api/v1/reports` | 登记一路平台回报（先核对 config blob 平台） | 200 / 409 / 422 / 502 |
| `POST /api/v1/publish` | 按批次合版并写 OCI 索引 | 200 / 409 / 422 / 502 |
| `GET /api/v1/batches?repository=&batch=` | 查询批次内存状态与上次成功引用 | 200 / 422 |
| `POST /mutate` | 原有准入改写（保持不变） | 200 |
| `GET /healthz` | 健康检查 | 200 |

回报请求体：

```json
{
  "repository": "apps/merged",
  "batch": "b-2026-1",
  "platform": "linux/amd64",
  "attempt": 1,
  "digest": "sha256:..."
}
```

发布响应（成功）：

```json
{
  "status": "published",
  "repository": "apps/merged",
  "batch": "b-2026-1",
  "reference": "localhost:5000/apps/merged@sha256:9f2c...",
  "index_digest": "sha256:9f2c...",
  "last_good_reference": "localhost:5000/apps/merged@sha256:9f2c...",
  "reason": null,
  "reports": { "linux/amd64": {...}, "linux/arm64": {...} }
}
```

冲突/失败时 HTTP 状态为 409/502，业务字段放在 `detail` 内，
`last_good_reference` 给出仍可拉取的上一版引用。

### 用 Docker Compose 试跑合版

```bash
docker compose up -d --build

# 1. 两路 CI 各自构建、推送子清单并回报（脚本会把摘要 POST 给合版 API）
python3 scripts/ci_build_report.py --repository apps/merged \
    --batch b-2026-1 --platform linux/amd64 --attempt 1
python3 scripts/ci_build_report.py --repository apps/merged \
    --batch b-2026-1 --platform linux/arm64 --attempt 1

# 2. 查询批次状态（应看到两路 attempt=1 的有效回报）
python3 scripts/publish_batch.py --repository apps/merged \
    --batch b-2026-1 --status

# 3. 合版，拿到可拉取引用
python3 scripts/publish_batch.py --repository apps/merged --batch b-2026-1
# -> docker pull localhost:5000/apps/merged@sha256:...

# 4. 模拟重试：amd64 以更大尝试号重新构建回报，再次发布生成新索引
python3 scripts/ci_build_report.py --repository apps/merged \
    --batch b-2026-1 --platform linux/amd64 --attempt 2
python3 scripts/publish_batch.py --repository apps/merged --batch b-2026-1
```

也可以直接用 curl 与 `samples/merge/` 下的样例（注意样例里的摘要需替换成
仓库中真实存在的子清单摘要，否则登记期核对会返回 422）：

```bash
curl -s -X POST http://localhost:8080/api/v1/reports \
    -H 'Content-Type: application/json' \
    --data @samples/merge/report-amd64-1.json
curl -s -X POST http://localhost:8080/api/v1/publish \
    -H 'Content-Type: application/json' \
    --data @samples/merge/publish.json
# 同尝试号不同摘要 -> 409：
curl -s -X POST http://localhost:8080/api/v1/reports \
    -H 'Content-Type: application/json' \
    --data @samples/merge/report-amd64-1-conflict.json
```

### 并发与一致性说明

- 状态存储 `MergeStore` 用单把可重入锁保护；registry 的 HTTP 往返（核对、
  写索引）在锁外执行，因此写索引期间回报接口不被阻塞。
- 发布基于进入时的回报快照（basis）；写完索引重新拿锁提交，任一路出现更高
  尝试号或基准摘要变化即整体判冲突，不更新“上次成功引用”。
- 进程重启后内存状态丢失，但已经推送进 registry 的索引与子清单仍然存在；
- 标签从不被本服务修改，合版产物只能通过摘要引用拉取。

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
| `PUBLIC_REGISTRY_HOST` | `localhost:5000` | 合版 API 返回的可拉取引用使用的 host |
| `REGISTRY_TIMEOUT` | `5` | 单次仓库 HTTP 请求超时（秒） |

注意：补丁里保留镜像原始 host（如 `localhost:5000`），只把标签换成
`@sha256:...`；webhook 自身通过 `REGISTRY_UPSTREAM` 访问仓库。

## 测试

测试不需要 Docker 或第三方库，只用 Python 标准库：

```bash
python3 -I tests/run.py
# 68 tests OK：引用解析、平台选择、索引解析、请求内只解析一次、
# 标签改指重新查询、缺平台/缺子清单/摘要不一致拒绝、真实 HTTP 端到端；
# 合版：入参校验、尝试号优胜/同号冲突/迟到回报、config blob 平台核对、
# 未收齐拒发、写索引期间换报冲突与回退、失败返回上次成功引用、重试合版、
# 合版全链路真实 HTTP 端到端
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
- 合版状态仅保存在单进程内存：多副本部署不会共享回报状态，进程重启后
  “上次成功引用”与待发布回报丢失（registry 中的索引/子清单不受影响）；
  生产化需要外接存储或改为单实例 + 持久化。
- 合版只支持固定的 `linux/amd64` 与 `linux/arm64` 两路，且不接受嵌套索引；
  回报没有鉴权与签名校验，依赖本地可信网络。
