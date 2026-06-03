FROM node:22-alpine AS builder

WORKDIR /app/apps/web

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .

# Build the SPA. We use a relative API base so the production reverse
# proxy (Caddy) can route /api/* to the api service without rebuilds when
# the domain changes.
ENV VITE_API_BASE_URL=""
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=builder /app/apps/web/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
