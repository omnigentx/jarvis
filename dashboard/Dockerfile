# Stage 1: Build Vue dashboard
FROM node:20-alpine AS build

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
FROM nginx:alpine AS final

# Copy built dashboard
COPY --from=build /app/dist /usr/share/nginx/html

# Nginx config with reverse proxy to backend
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
