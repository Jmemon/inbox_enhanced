
## Auth & deployable skeleton
covers the auth/session/single-origin/encryption plumbing that the rest of the app sits on top of. inbox/sync/classification/buckets concerns live in the other specs and reference back here.

### API server
python fastapi

### Auth endpoints
no auth required on /auth/login and /auth/callback. /auth/me and /auth/logout require session cookie.

GET /auth/login
 - 302 to google authorize URL, sets short-lived signed state cookie

GET /auth/callback?code=&state=
 - validates state, exchanges code, upserts user, creates session, sets session cookie, 302 to /
 - on ?error= from google, 302 to /?authError=<reason>

GET /auth/me
 - 200 {id, email, name} or 401

POST /auth/logout
 - 204, revokes session row + clears cookie

### Auth dependency
fastapi dependency get_current_user:
 - read session cookie -> look up sessions (not revoked, not expired) -> join users -> return user or 401
 - applied to every non-/auth endpoint
 - updates lastSeenAt as a side effect

### Auth flow (oauth 2.0 + cookie session)
backend is the confidential oauth client. frontend never touches tokens.

login start (GET /auth/login)
 - generate random state, set as short-lived signed cookie (10 min ttl, signed with SESSION_SECRET)
 - 302 to google authorize URL
 - scopes: openid email profile https://www.googleapis.com/auth/gmail.readonly
 - access_type=offline, prompt=consent so we reliably get a refresh token

callback (GET /auth/callback?code=&state=)
 - verify state matches signed cookie, clear the state cookie
 - exchange code at google token endpoint for {access_token, refresh_token, expires_in}
 - call userinfo with access_token to get {email, name}
 - upsert users row by email. encrypt and persist refreshToken + accessToken + expiresAt
 - insert sessions row, set its id as session cookie (HttpOnly, Secure, SameSite=Lax)
 - 302 to / (single-origin, fastapi serves the spa at /)

session check (GET /auth/me)
 - read session cookie, look up sessions row where revokedAt is null and expiresAt > now()
 - 200 {id, email, name} or 401
 - update lastSeenAt

logout (POST /auth/logout)
 - set revokedAt on sessions row, clear cookie. 204.

token refresh
 - any backend call that needs gmail goes through a helper that checks gmail_accessTokenExpiresAt, refreshes via refreshToken if expired, persists the new access token.
 - if google returns invalid_grant on refresh: null out gmail tokens for that user. next /auth/me returns 401, frontend bounces to login.

failure modes
 - user denies consent: callback gets ?error=access_denied, 302 to /?authError=denied
 - state mismatch or missing: 400, do not exchange code
 - session expired or revoked: 401 from anything authed, frontend bounces to login

### Cookies
session cookie: HttpOnly, Secure (prod), SameSite=Lax, path=/
no cookie domain set on railway-issued hosts (public suffix list). only set COOKIE_DOMAIN once on a custom domain.
no CORS middleware needed in v1 since frontend + api are same-origin. add one later only if we split origins.

### Proxy headers
railway terminates tls at the edge; the container sees plain http. launch uvicorn with --proxy-headers --forwarded-allow-ips='*' so request.url.scheme reflects the public https. otherwise any code that branches on scheme (eg deciding to send Secure cookies in dev vs prod) will misbehave.

### Static files (spa hosting)
fastapi serves the built react bundle from the same origin as the api. avoids cross-site cookie problems on railway's *.up.railway.app subdomains (they're on the public suffix list, so cross-subdomain SameSite=Lax cookies don't ride on fetch). custom domain not required for v1.
 - mount StaticFiles at /assets (hashed bundle output from bun build)
 - catch-all GET handler for non-/api, non-/auth, non-/assets paths returns index.html (so client-side routing works on refresh)
 - api routes live under /api/* (or just at root, but /api/* keeps the catch-all simple)

### Env
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
SESSION_SECRET (signs oauth state cookie)
SESSION_TTL_SECONDS (default 30 days)
ENCRYPTION_KEY (encrypts gmail tokens at rest. set once, never rotate casually - rotating strands every stored refresh token)
COOKIE_DOMAIN (only when on a custom domain; leave unset on railway-issued hosts)

### Frontend auth state
spa is served by fastapi from the same origin as the api, so all requests are same-origin and the session cookie rides along by default.
on app mount: fetch('/auth/me')
 - 200 -> store user, render home
 - 401 -> render login
 - while pending -> splash

401 from any authed call -> clear user, bounce to login.

### Splash
shown while /auth/me is pending on mount, and during the post-callback redirect when we land back on / before /auth/me resolves.
full-viewport, centered. app name/logo + a subtle spinner. nothing else.

### Login screen
shown when /auth/me returns 401.
full-viewport, centered card.
 - app name + one-line tagline
 - "sign in with google" button (google's brand button, white bg + colored G mark)
 - if url has ?authError=<reason>, render a small inline error above the button (eg "sign-in cancelled" for denied)
button click does window.location.assign('/auth/login'). backend handles the whole oauth dance and 302s back to / with cookie set. on return, app remounts -> /auth/me -> home.

### Logout
"sign out" in the top-bar menu -> POST /auth/logout -> clear user -> route to login.

### Postgres tables

users
 - id: string uuid
 - email: string
 - name: string (from google userinfo, for display)
 - gmail_refreshToken: text, encrypted at rest with ENCRYPTION_KEY. obtained on first oauth (access_type=offline + prompt=consent), used to mint access tokens later
 - gmail_accessToken: text, encrypted at rest. short-lived, refreshed on demand
 - gmail_accessTokenExpiresAt: timestamp
 - gmail_lastHistoryId (read/written by sync workers; see psql spec for context)

sessions (cookie sessions, opaque id as cookie value)
 - id: random urlsafe string (>=32 bytes), this is the cookie value. HttpOnly, Secure, SameSite=Lax
 - userId: fk to users(id)
 - createdAt: timestamp
 - expiresAt: timestamp. fixed ttl from SESSION_TTL_SECONDS (default 30 days). queries filter expiresAt > now() and revokedAt is null
 - lastSeenAt: timestamp, updated on each authed request. could later be used for sliding refresh, not doing that yet
 - revokedAt: timestamp nullable, set on logout

gmail oauth tokens go on users (1:1, KISS). encrypt at rest because refresh tokens are bearer creds.
in future could split accounts/users concepts.

### Deployment notes (auth-relevant)
single-origin setup. fastapi serves the built react bundle as static files alongside the api routes, so frontend and api share one origin. avoids the cross-site cookie issue with railway's *.up.railway.app subdomains. custom domain not required for v1.
 - uvicorn launched with --proxy-headers --forwarded-allow-ips='*' so request.url.scheme reflects the public https (railway terminates tls at the edge)
 - do not set cookie domain on railway-issued hosts (public suffix list rejects it). only set COOKIE_DOMAIN once on a custom domain.
 - ENCRYPTION_KEY is set once in railway variables and never rotated casually. rotating it strands every stored gmail refresh token and forces all users to re-auth.

(see flows spec for the full railway services list and build pipeline.)
