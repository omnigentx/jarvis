# Stage 1: Build Vue frontend
# Track the active LTS line (22.x) on glibc — NOT a frozen minor and NOT
# alpine. Major-pin (not minor-pin) so the build stage keeps receiving
# 22.x security patches automatically; Node 20 went EOL ~2026-04.
#
# MUST be glibc (slim) + Node 22, for two reasons:
#  1. Vite 8 bundles with rolldown, whose native binding is an optional,
#     platform-specific dep (@rolldown/binding-linux-*). `npm ci` fails to
#     install it on alpine/musl (any npm) -> `npm run build` dies with
#     `MODULE_NOT_FOUND ... rolldown/dist/shared/binding-*.mjs`. glibc fixes it.
#  2. vite + rolldown need Node >= 20.19; @babel/generator (via
#     @vitejs/plugin-vue) wants >= 22.18 — Node 22 satisfies all three with
#     no EBADENGINE warnings, where 20.19 only just cleared the binding.
# Verified by building the real context locally (linux/amd64) across
# alpine/slim x node 20.18/20.19/20.20/22.
FROM node:22-slim AS build

WORKDIR /app

# Copy package files for dependency caching
COPY package.json package-lock.json ./

# Install dependencies
RUN npm ci

# Copy rest of source
COPY . .

# Build production bundle
RUN npm run build

# Stage 2: Serve with nginx
# Minor-pin so the runtime image is reproducible across rebuilds.
FROM nginx:1.27-alpine AS final

# Copy built frontend
COPY --from=build /app/dist /usr/share/nginx/html

# Nginx config with reverse proxy to backend
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
