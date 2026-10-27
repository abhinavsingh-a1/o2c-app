# Stage 1: build the static files with Node
FROM node:25-alpine AS build
ARG PNPM_VERSION=10
RUN npm install -g pnpm@${PNPM_VERSION}
WORKDIR /repo
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY ui/package.json ui/
RUN pnpm install --frozen-lockfile
COPY ui ui
RUN pnpm --dir ui build

# Stage 2: serve them with nginx, as a non-root user
FROM nginxinc/nginx-unprivileged:1.31-alpine
COPY ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY ui/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /repo/ui/dist /usr/share/nginx/html
# The base image already runs as nginx (uid 101); stated explicitly for scanners
USER 101
EXPOSE 8080
