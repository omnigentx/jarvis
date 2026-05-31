# Stage 1: Build Vue frontend
# Minor-pin so a rebuild a month from now picks up the same Node line.
# Bump manually when upstream issues a new LTS minor.
FROM node:20.18-alpine AS build

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
