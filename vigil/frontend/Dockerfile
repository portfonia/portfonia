FROM oven/bun:1.4.0-alpine AS deps
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile


FROM oven/bun:1.4.0-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ARG BACKEND_URL=http://vigil-backend:8000
ENV BACKEND_URL=$BACKEND_URL

ARG NEXT_PUBLIC_SUPABASE_URL=https://auth.portfonia.com
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
ARG NEXT_PUBLIC_PORTFONIA_ORIGIN=https://portfonia.com
ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL
ENV NEXT_PUBLIC_SUPABASE_ANON_KEY=$NEXT_PUBLIC_SUPABASE_ANON_KEY
ENV NEXT_PUBLIC_PORTFONIA_ORIGIN=$NEXT_PUBLIC_PORTFONIA_ORIGIN

RUN test -n "$NEXT_PUBLIC_SUPABASE_ANON_KEY" || \
    (echo "NEXT_PUBLIC_SUPABASE_ANON_KEY build arg is required" >&2 && exit 1)

RUN bun run build


FROM node:22-alpine AS runner
WORKDIR /app

RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
