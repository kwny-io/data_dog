# data_dog 前端镜像（Next.js 15 控制台）
#
# 构建（在 web/ 目录下）：
#   docker build -t data-dog-web:0.1.0 .
#
# 运行：
#   docker run --rm -p 3000:3000 data-dog-web:0.1.0
#
# 与后端容器一样跑非 root，且只暴露一个端口。
#
# 注意：NEXT_PUBLIC_* 变量在 **构建期** 被内联进浏览器 bundle，不是运行期读取。
# 所以它们以 build-arg 传入（下面 ARG），改了要重新 build，不是 restart。

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

# 生产：前端与后端同源（Caddy 反代），所以 base URL 留空表示同源 /v1/*。
# 本地/分域部署时在 build 时覆盖，例如
#   --build-arg NEXT_PUBLIC_API_BASE_URL=https://dog.example.com
ARG NEXT_PUBLIC_API_BASE_URL=""
ARG NEXT_PUBLIC_DEFAULT_TENANT="tenant-demo"
ARG NEXT_PUBLIC_API_KEY=""
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
