FROM node:24.21.0-bookworm-slim AS build
WORKDIR /app
RUN corepack enable
COPY frontend/admin/package.json frontend/admin/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/admin/index.html frontend/admin/tsconfig.json frontend/admin/vite.config.ts ./
COPY frontend/admin/src ./src
RUN pnpm build

FROM nginx:1.28-alpine
COPY deploy/proxy/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html/admin
EXPOSE 80
