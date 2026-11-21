# syntax=docker/dockerfile:1

# ---------- build stage: compile the React app ----------
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Optional build-time default; API_BASE_URL at runtime takes precedence.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

# ---------- runtime stage: static files served by nginx ----------
FROM nginx:1.27-alpine AS runtime
ENV PORT=3000 \
    API_BASE_URL=""
# nginx's official entrypoint runs envsubst on /etc/nginx/templates/*.template
# and executes /docker-entrypoint.d/*.sh before starting.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY docker/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY docker/40-env-config.sh /docker-entrypoint.d/40-env-config.sh
RUN chmod +x /docker-entrypoint.d/40-env-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- "http://127.0.0.1:${PORT}/" >/dev/null || exit 1
STOPSIGNAL SIGQUIT
