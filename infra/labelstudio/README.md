# Label Studio Runtime Notes

Label Studio CE is deployed as an external service in the compose stacks and is linked from the admin UI through the reverse proxy.

Runtime captures and uploaded dataset assets are submitted as Label Studio local-file URLs, for example:

```text
/data/local-files/?d=runtime/captures/<channel>/<project>/<mode>/<day>/<file>.jpg
```

The compose stacks therefore enable Label Studio local file serving with `/srv/cv-platform` as the document root and mount only the platform paths that Label Studio needs to read:

- `/srv/cv-platform/runtime` for channel captures
- `/srv/cv-platform/control/uploads` for uploaded dataset assets

The reverse proxy serves `/data/local-files/*` directly from the mounted platform asset roots with permissive CORS headers. `/label-studio/data/local-files/*` remains available for older imported tasks. `/data/upload/*` still proxies to Label Studio for Label Studio-owned upload assets.
