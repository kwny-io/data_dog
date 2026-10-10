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
FROM node:22-alpine AS deps
WORKDIR /app
# 只拷贝清单 → 依赖层可缓存，改代码不必重装依赖
COPY package.json package-lock.json ./
RUN npm ci

# ---------------------------------------------------------------- builder
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# 生产：前端与后端同源（Caddy 反代），控制台请求同源 /api/dd/*。
# 本地/分域部署时在 build 时覆盖（仅**非机密**的基路径/超时；密钥不要走这里）：
#   --build-arg NEXT_PUBLIC_API_BASE_URL=https://dog.example.com/api/dd
ARG NEXT_PUBLIC_API_BASE_URL=""
ARG NEXT_PUBLIC_INVESTIGATE_TIMEOUT_MS="110000"
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# ---------------------------------------------------------------- runner
FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# standalone 产物是一个自带最小 node_modules 的服务器，不需要整棵依赖树
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

# 非 root 运行（最小权限）；nextjs 用户由官方镜像预置
USER nextjs

EXPOSE 3000

# 容器自检：探首页（探测目标是容器内固定地址，非用户可控 URL）
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
