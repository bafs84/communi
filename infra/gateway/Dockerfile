FROM node:22.19-alpine AS site-builder
ENV ASTRO_TELEMETRY_DISABLED=1
WORKDIR /build/site
COPY apps/site/package.json apps/site/package-lock.json ./
RUN npm ci
COPY apps/site/ ./
COPY apps/shared/ /build/shared/
ARG PUBLIC_AUTH0_DOMAIN
ARG PUBLIC_AUTH0_CLIENT_ID
ARG PUBLIC_AUTH0_AUDIENCE
ARG PUBLIC_AUTH0_REDIRECT_URI
ARG PUBLIC_API_BASE_URL
ENV PUBLIC_AUTH0_DOMAIN=$PUBLIC_AUTH0_DOMAIN \
    PUBLIC_AUTH0_CLIENT_ID=$PUBLIC_AUTH0_CLIENT_ID \
    PUBLIC_AUTH0_AUDIENCE=$PUBLIC_AUTH0_AUDIENCE \
    PUBLIC_AUTH0_REDIRECT_URI=$PUBLIC_AUTH0_REDIRECT_URI \
    PUBLIC_API_BASE_URL=$PUBLIC_API_BASE_URL
RUN npm run build

FROM node:22.19-alpine AS ckw-builder
WORKDIR /build/ckw
COPY apps/ckw-web/package.json apps/ckw-web/package-lock.json ./
RUN npm ci
COPY apps/ckw-web/ ./
COPY apps/shared/ /build/shared/
RUN npm run build

FROM nginx:1.29.1-alpine
COPY --from=site-builder /build/site/dist /usr/share/nginx/html/site
COPY --from=ckw-builder /build/ckw/dist /usr/share/nginx/html/ckw
COPY infra/gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY infra/gateway/99-runtime-config.sh /docker-entrypoint.d/99-runtime-config.sh
RUN chmod 755 /docker-entrypoint.d/99-runtime-config.sh \
    && chown -R nginx:nginx /usr/share/nginx/html
EXPOSE 8080
