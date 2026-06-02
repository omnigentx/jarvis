# Stage 1: Build Vue frontend
# Minor-pin so a rebuild a month from now picks up the same Node line.
# Bump manually when upstream issues a new LTS minor.
#
# MUST be glibc (slim) + Node >= 20.19, NOT alpine and NOT 20.18:
# Vite 8 bundles with rolldown, whose native binding is an optional,
# platform-specific dep (@rolldown/binding-linux-*). `npm ci` fails to
# install that binding on (a) alpine/musl (any npm) and (b) Node 20.18
# even on glibc, so `npm run build` dies with
# `MODULE_NOT_FOUND ... rolldown/dist/shared/binding-*.mjs`. Node 20.19
# (which enables require(esm)) on glibc resolves it. Verified by building
# the real context locally across alpine/slim × node 20.18/20.19/20.20.
FROM node:20.19-slim AS build

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
