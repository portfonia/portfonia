FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# --legacy-peer-deps: next-intl's nested @swc/core declares an OPTIONAL
# peer dependency on @swc/helpers (peerDependenciesMeta.optional: true) that
# the committed lockfile correctly never resolves -- `npm install` honors
# "optional" and skips it with no error, but `npm ci`'s stricter npm 7+
# peer-conflict validation flags the same absence as a hard failure. This
# disables that specific over-strict check (npm 6 peer semantics: warn,
# don't block) without touching package.json/package-lock.json version
# consistency enforcement, which `ci` still fully performs. See issue #227.
RUN npm ci --legacy-peer-deps


FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# BACKEND_URL is read inside next.config.ts's rewrites(); baked in at build
# time regardless of whether Next re-reads it at runtime (see Dockerfile
# comment in docker-compose.yml — same value is also set as a runtime env
# var on the container so both cases are covered without depending on
# Next's internal evaluation timing).
ARG BACKEND_URL=http://backend:8000
ENV BACKEND_URL=$BACKEND_URL

# NEXT_PUBLIC_* vars are statically inlined into both the client bundle and
# server code by `next build` — there is no runtime alternative, they MUST
# be present here (Ring 1-B design doc §7.3(2)/(6), B5). SUPABASE_URL here
# is deliberately NOT the same value as the backend's SUPABASE_URL Setting:
# the backend talks to the raw Supabase project host directly (no
# reachability concern — verification runs on our own server, §6.5 point
# 2); the browser talks through the auth.portfonia.com Caddy reverse proxy
# (mainland-reachability workaround, §2.7/§6.10). Getting these two swapped
# silently breaks login for exactly the users the proxy exists for.
ARG NEXT_PUBLIC_SUPABASE_URL=https://auth.portfonia.com
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL
ENV NEXT_PUBLIC_SUPABASE_ANON_KEY=$NEXT_PUBLIC_SUPABASE_ANON_KEY

# docker-compose.yml fails closed on a missing NEXT_PUBLIC_SUPABASE_ANON_KEY
# (${SUPABASE_ANON_KEY:?required}), but a raw `docker build` bypasses that
# entirely — the ARG above has no default, so an omitted build arg silently
# bakes an empty key into an otherwise "successful" image (blacktomb42
# review, PR #185). Fail here too, before the expensive build step runs.
RUN test -n "$NEXT_PUBLIC_SUPABASE_ANON_KEY" || \
    (echo "NEXT_PUBLIC_SUPABASE_ANON_KEY build arg is required" >&2 && exit 1)

RUN npm run build


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
