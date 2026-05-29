
> the inbox sync flows (initial load, polling, sse, message parser) moved to
> specs/04_30_2026-project_minimum-homepage.md.
> auth flow lives in the auth spec.

## Creating a new custom bucket
(see workers spec for the classification pipeline + custom-bucket thinking)


## Deployment
Railway

> single-origin / cookie / proxy-header / encryption-key notes for deployment
> live in the auth spec. this section is the service topology and build pipeline.

services:
 - api server (also serves the spa bundle)
 - worker service (celery, runs sync + classification jobs)
 - beat service (celery beat scheduler, replicas=1)
 - redis service (job queue + pub/sub + active-user registry)
 - postgres service

build:
 - frontend built with bun, output copied into api image (eg /app/static)
 - fastapi mounts StaticFiles for /assets and a catch-all that returns
   index.html for unknown non-/api paths (spa router)

runtime notes:
 - bind to $PORT
