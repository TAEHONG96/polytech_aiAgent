FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS dependencies
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --include=optional --no-audit --no-fund --fetch-retries=3 \
    && node --input-type=module -e "await import('rolldown')"
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src

FROM dependencies AS development
CMD ["npm", "run", "dev"]

FROM dependencies AS build
RUN npm run build

FROM nginx:1.30-alpine@sha256:901e593c95dc85f5a729f5765516321366ef4ac401821431d408897e95287d0a AS proxy
COPY nginx/log-format.conf /etc/nginx/conf.d/00-polytech-log.conf
COPY nginx/proxy.conf /etc/nginx/polytech/proxy.conf
COPY nginx/request-errors.conf /etc/nginx/polytech/request-errors.conf
COPY nginx/training-signup.conf /etc/nginx/polytech/training-signup.conf

FROM proxy AS local
COPY nginx/local.conf /etc/nginx/conf.d/default.conf

FROM proxy AS static
COPY nginx/static.conf /etc/nginx/polytech/static.conf
COPY nginx/registration-disabled.conf /etc/nginx/polytech/registration.conf
COPY --from=build /web/dist /usr/share/nginx/html

FROM static AS production
COPY nginx/production.conf /etc/nginx/conf.d/default.conf

FROM static AS tunnel-config
COPY nginx/tunnel.conf /etc/nginx/conf.d/default.conf

FROM tunnel-config AS training-tunnel
COPY nginx/training-signup.conf /etc/nginx/polytech/registration.conf

FROM tunnel-config AS tunnel
