Optional: CA certificate(s) for a *verified* TLS connection to the database (see docs/SUPABASE.md, "Verify the server certificate").

1. Save the CA certificate here, e.g. `supabase-ca.crt` (public information - it is not a secret).
2. In `deploy/.env` set `?ssl=verify-full` at the end of `DATABASE_URL` and `PGSSLROOTCERT=/certs/supabase-ca.crt`.
3. `docker compose up -d`

This folder is mounted read-only at `/certs` in the api, bot and migrate containers.
