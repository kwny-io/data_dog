# data_dog 前端镜像（Next.js 15 控制台）
#
# 构建（在 web/ 目录下）：
#   docker build -t data-dog-web:0.1.0 .
#
# 运行（**服务端**变量在运行期注入，不是构建期）：
#   docker run --rm -p 3000:3000 \
#     -e DD_WEB_API_BASE_URL=http://app:8000 \
#     -e DD_WEB_API_KEY=<与后端 DD_API_KEY 一致> \
#     -e DD_WEB_TENANT=tenant-demo \
#     -e DD_WEB_BASIC_AUTH=ops:<口令> \
#     data-dog-web:0.1.0
#
# 与后端容器一样跑非 root，且只暴露一个端口。
#
# ⚠ 关于密钥：浏览器**不持有**任何凭据——控制台只请求同源 /api/dd/*，由服务端代理
#   （app/api/dd/[...path]）附加 X-API-Key / X-Tenant-Id。所以：
#     - 密钥走**运行期**环境变量（上面 -e 的 DD_WEB_*），绝不要用 build-arg 传：
#       build-arg 会留在镜像历史里（`docker history` 可见），等于密钥进镜像；
#     - 更不要用 NEXT_PUBLIC_*：Next.js 会把它们内联进浏览器 bundle（这正是被修掉的泄露）。
#   `NEXT_PUBLIC_*` 现在只剩"代理基路径/超时"这类**非机密**构建期常量。

# ---------------------------------------------------------------- deps
# 基镜像可覆盖：默认官方 alpine；企业内网/离线环境用
#   docker build --build-arg NODE_IMAGE=<镜像仓库>/library/node:22-bookworm-slim
# 把地址钉在组织镜像仓库（与 CI 腿镜像同一原则：加速器/镜像源属运行环境，不写进仓库）。
ARG NODE_IMAGE=node:22-alpine

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
# 企业内网/加速器：npm ci 前把 registry 指向内部镜像（只影响构网，不影响产物）。
#    --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=""
# 只拷贝清单 → 依赖层可缓存，改代码不必重装依赖
COPY package.json package-lock.json ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm ci

# ---------------------------------------------------------------- builder
FROM ${NODE_IMAGE} AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# 生产：前端与后端同源（Caddy 反代），控制台请求同源 /api/dd/*。
# 本地/分域部署时在 build 时覆盖（仅**非机密**的基路径/超时；密钥不要走这里）：
#   --build-arg NEXT_PUBLIC_API_BASE_URL=https://dog.example.com/api/dd
#
# 默认值必须是 `/api/dd` 而不是空串：这个值会被内联进浏览器包，空串会让控制台把
# `/v1/candidates` 打到页面自己的源上 → 404 → 界面只显示 0 而且不报错（实测踩到）。
ARG NEXT_PUBLIC_API_BASE_URL="/api/dd"
ARG NEXT_PUBLIC_INVESTIGATE_TIMEOUT_MS="110000"
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# ---------------------------------------------------------------- runner
FROM ${NODE_IMAGE} AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# standalone 产物是一个自带最小 node_modules 的服务器，不需要整棵依赖树
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

# 非 root 运行（最小权限）。官方 node 镜像预置的就是 **`node`（uid 1000）**；
# 曾误写成 `nextjs`（那是 next.js 官方示例里自己 useradd 出来的用户，官方镜像里不存在），
# 结果是镜像能构建、`docker run` 却报 "unable to find user nextjs" 直接起不来。
USER node

EXPOSE 3000

# 容器自检：探首页（探测目标是容器内固定地址，非用户可控 URL）
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
