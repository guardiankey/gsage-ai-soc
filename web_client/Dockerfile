# syntax=docker/dockerfile:1.6
# Stage 1: build
FROM node:22-alpine AS builder
WORKDIR /app

COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm ci

COPY . .
RUN npm run build

# Stage 2: serve with Nginx
FROM nginx:1.27-alpine AS runner

# Built SPA assets.
COPY --from=builder /app/dist /usr/share/nginx/html

# http-context map (WebSocket / SSE Connection upgrade).
COPY nginx.upgrade-map.conf /etc/nginx/conf.d/00-upgrade-map.conf

# Both profiles are baked in; the entrypoint picks one at runtime via
# GSAGE_NGINX_PROFILE (dev|prod, default: prod).
RUN mkdir -p /etc/nginx/gsage-profiles
COPY nginx.conf       /etc/nginx/gsage-profiles/nginx.dev.conf
COPY nginx.prod.conf  /etc/nginx/gsage-profiles/nginx.prod.conf

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV GSAGE_NGINX_PROFILE=prod
EXPOSE 80
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
