# Communi.ch Website

Static Astro site for the `Communi.ch` research project, including a client-side Auth0-protected participant area under `/raum/`.

## Commands

| Command | Action |
| :-- | :-- |
| `npm install` | Install local dependencies |
| `npm run dev` | Start local development on a local Astro port |
| `npm run build` | Generate the static site in `dist/` |
| `npm run preview` | Preview the production build locally |
| `npm run lint:check` | Run ESLint without modifying files |
| `npm run typecheck` | Run TypeScript checks |

## Project configuration

Global site metadata and navigation live in `src/config/site.ts`.

## Auth0 setup

The participant area is static and uses the official Auth0 SPA JS browser client from the Auth0 CDN. Copy `.env.example` to `.env` and set:

```bash
PUBLIC_AUTH0_DOMAIN=communi.eu.auth0.com
PUBLIC_AUTH0_CLIENT_ID=yqCNwDGot4cwigL3TM0iEWc9j5QMTgq6
PUBLIC_AUTH0_AUDIENCE=
PUBLIC_AUTH0_REDIRECT_URI=
PUBLIC_API_BASE_URL=http://127.0.0.1:4100
PUBLIC_GRAPHQL_API_URL=
```

If `PUBLIC_AUTH0_REDIRECT_URI` is empty, the app uses the current browser origin automatically, for example `http://localhost:4321/raum/` in local development.

If `PUBLIC_API_BASE_URL` and `PUBLIC_AUTH0_AUDIENCE` are both set, `/raum/` stores profile data, uploads, and questions through the Rust REST API instead of keeping everything only in local browser storage. `PUBLIC_GRAPHQL_API_URL` is kept only as a legacy fallback for the older Node API.

Do not place the Auth0 client secret into this Astro site. This project is a public browser app, so the secret would be exposed to every visitor. If the secret has been shared outside Auth0, rotate it.

In Auth0, the callback URL still has to be allowlisted explicitly. Add the exact local URL you are using, for example:

- callback URL: `http://localhost:4322/raum/`
- logout URL: `http://localhost:4322/raum/`
- web origin: `http://localhost:4322`

If your dev port changes, either update the Auth0 allowlist accordingly or pin Astro to a fixed port when you start it, for example `npm run dev -- --port 4322`.

For production, replace those URLs with the final `https://.../raum/` address and origin.

## Static export

Build the site and upload the generated `dist/` folder to your server:

```bash
npm run build
```

## Current scope of `/raum/`

- Auth0 login and logout
- Municipality and DSO selection based on `municipalities-data-2026.csv`
- CSV upload with first-pass energy and solar-proxy summary
- Private question list per authenticated user
- Optional municipality signals and opt-in visible contacts

Without GraphQL configuration, the workspace falls back to local browser storage.
