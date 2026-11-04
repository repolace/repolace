-- Idempotent: deploy.sh runs this on every deploy, as the postgres superuser, against database `repolace`.
-- The table mirrors alembic revision 0017 (api/alembic/versions/0017_add_waitlist_signups.py). Keep them equal.
-- The role's password is never in this file: deploy.sh passes it in the environment variable
-- REPOLACE_SITE_PW and psql reads it with \getenv.
\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS waitlist_signups (
    id uuid NOT NULL,
    email varchar NOT NULL,
    source varchar DEFAULT 'site' NOT NULL,
    consent boolean NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT ck_waitlist_signups_email_length CHECK (char_length(email) <= 254),
    PRIMARY KEY (id),
    UNIQUE (email)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'repolace_site') THEN
        CREATE ROLE repolace_site LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM repolace_site;
GRANT USAGE ON SCHEMA public TO repolace_site;
GRANT INSERT ON waitlist_signups TO repolace_site;

\getenv site_pw REPOLACE_SITE_PW
\if :{?site_pw}
ALTER ROLE repolace_site PASSWORD :'site_pw';
\endif
