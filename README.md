# Financial Help Desk & Fund Requisition System

A Django web application for submitting, reviewing, disbursing, and reconciling organizational fund requests. It includes a role-based dashboard, a multi-stage approval workflow, an authenticated REST API, and an append-only application audit history.

## Workflow

1. **Employee** creates a draft for the planned activity, adds the justification and amount, and submits it.
2. **Supervisor** reviews departmental need and activity fit.
3. **M&E Officer** checks the request against indicators and compliance standards.
4. **Program Lead** reviews program alignment and implementation readiness.
5. **Executive Director** provides final executive approval (and can approve, request revision, or reject).
6. **Finance** reviews the request, creates the voucher, selects bank transfer or petty cash, and records disbursement.
   Administrative activities are the only exception: submission or resubmission routes directly to Finance; all non-administrative requests must pass through each preceding approval stage.
7. **Requester** submits actual spend, an activity report, and receipt or invoice evidence. Any unspent balance is calculated for reconciliation.
8. **Internal Audit** validates the accountability evidence before closing the request. The final request status is **Completed**.

At any approval stage, an approver can request a revision or reject the request. Revision comments and every status transition are recorded in the request history. Final accountability reconciles the approved amount to actual spending.

## Features

- Role-aware dashboard and approval queue for employees, supervisors, M&E, program leads, executive directors, finance staff, Internal Audit, and administrators.
- Server-enforced status transitions, department-scoped supervisor approvals, and date-limited delegation.
- Accountability reports and receipt/invoice uploads, limited to PDF and common image formats, up to 15 MB per file.
- Read-only audit history and protected, authenticated file downloads.
- Internal Audit-only monthly, quarterly, and annual requisition reports with CSV export.
- Django admin for user account and workflow setup.
- REST API under `/api/` using Django session or HTTP Basic authentication.

## Requirements

- Python 3.10+
- SQLite for a local development database (included with Python), or PostgreSQL

## Local setup

```bash
git clone https://github.com/CUSTOMIZE-137/financial-help-desk-and-fund-requisition-system.git
cd financial-help-desk-and-fund-requisition-system
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py createsuperuser
python manage.py runserver
```

By default, the app uses SQLite at `db.sqlite3` and local development settings. Sign in at `http://127.0.0.1:8000/admin/` with the superuser, then create staff accounts. Set each account's role and department in the admin before testing the workflow. The dashboard is at `http://127.0.0.1:8000/`.

To configure environment variables, copy `.env.example` to `.env` and export its values in your shell. For PostgreSQL, set ` DATABASE_URL` (or `POSTGRES_URL`) or set `DB_ENGINE=django.db.backends.postgresql` and provide `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT`. Set a unique `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, and production `DJANGO_ALLOWED_HOSTS` before deployment. Do not use the example secret outside local development.

## Deploying on Vercel

The repository includes `vercel.json` and `build_files.sh` for serverless deployment on Vercel:

1. **Import the repository** into Vercel.
2. **Configure Environment Variables** in your Vercel Project Settings:
   - `DJANGO_SECRET_KEY` — a strong random secret key.
   - `DJANGO_DEBUG` — `False` (defaults to `False` automatically when `VERCEL=1`).
   - `DATABASE_URL` or `POSTGRES_URL` — your managed PostgreSQL connection string (e.g., Vercel Postgres / Neon or Supabase pooler URL), or `DB_ENGINE`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`.
   - `AWS_STORAGE_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optionally `AWS_S3_ENDPOINT_URL` / `AWS_S3_REGION_NAME` — S3-compatible object storage (e.g., AWS S3, Cloudflare R2, or Supabase Storage) for receipt, invoice, and attachment uploads.
   - `RUN_MIGRATIONS_ON_BUILD` — set to `true` to run `python manage.py migrate --noinput` automatically during Vercel builds.
3. **Deploy**: Vercel will execute `build_files.sh` to collect static assets into `staticfiles_build/static` and serve the Django WSGI application via `financial_helpdesk/wsgi.py`.

## Enrolling Users on Deployment

Administrators can enroll staff accounts with cryptographically generated temporary credentials, send invitation emails, and enforce a mandatory password change on first sign-in using either the **Admin Enrollment Web Interface** or the **CLI Management Script**:

1. **Admin Enrollment Web Interface (`/users/enroll/`)**:
   - Sign in as a superuser or `ADMIN` user and open **User enrollment** in the navigation bar (`/users/enroll/`).
   - Enroll individual users or upload a bulk CSV roster (`username,email,first_name,last_name,role,department`).
   - Re-issue temporary passwords and resend invitations for existing accounts at any time.

2. **CLI Management Command (`enroll_users`)**:
   ```bash
   # Enroll a single staff member and send an email invitation
   python manage.py enroll_users --username jdoe --email jdoe@example.org --first-name Jane --last-name Doe --role SUPERVISOR --department Programs

   # Bulk-enroll staff from a CSV file and export generated temporary credentials
   python manage.py enroll_users --csv staff.csv --output-credentials credentials.csv
   ```

3. **First-Login Password Reset Enforcement**:
   - Accounts enrolled with temporary credentials have `must_change_password=True`.
   - Upon first sign-in, the user is redirected to `/password-change/` (and blocked from all other web pages and API endpoints) until they set a distinct personal password.

## Production Growth & Operational Architecture

- **PostgreSQL & Connection Pooling**: Configure via `DATABASE_URL=postgresql://user:pass@host:5432/dbname` or `DB_*` variables with persistent connection reuse (`DB_CONN_MAX_AGE=60`), automatic connection health checks (`DB_CONN_HEALTH_CHECKS=True`), and SSL options (`DB_SSLMODE`).
- **Shared & S3-Compatible Object Storage**: Set `STORAGE_BACKEND=shared_fs` for multi-worker POSIX/NFS/EFS volumes or `STORAGE_BACKEND=s3` (with `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_REGION_NAME`, `AWS_S3_ENDPOINT_URL`) for AWS S3, MinIO, Cloudflare R2, or DigitalOcean Spaces with private objects and presigned URLs.
- **Query Optimization & Indexing**: Composite database indexes on `Requisition`, `RequisitionLog`, `User`, and `BackgroundTask`, single-query `aggregate()` dashboard counters, `select_related`/`prefetch_related` preloading, and `bulk_create` line-item persistence.
- **Caching**: Redis cache support via `REDIS_URL` (falling back to `LocMemCache` locally) with signal-driven cache version invalidation for delegation lookups, dashboard counts, and generated requisition PDFs.
- **Background Processing (`run_worker`)**: Durable `BackgroundTask` queue supporting `BACKGROUND_TASK_MODE=db_queue|thread|eager` for asynchronous enrollment invitations, workflow notifications, and PDF cache pre-warming with exponential backoff retries (`python manage.py run_worker`).
- **Operational Infrastructure**: Liveness (`/healthz/`) and readiness (`/readyz/`) probes, structured logging, reverse-proxy TLS/HSTS headers, and multi-service `Dockerfile` + `docker-compose.yml` (`db`, `redis`, `web`, `worker`).

## Tests and checks

```bash
python manage.py check
python manage.py test
```

## Main web routes

- `/` — dashboard
- `/users/enroll/` — administrator user enrollment, CSV import, and invitation management
- `/password-change/` — first-login and account password reset
- `/approval-queue/` — actions available to the signed-in user
- `/reports/internal-audit/` — Internal Audit reporting and CSV export
- `/requisitions/new/` — create a draft
- `/requisitions/<id>/` — request details and audit history
- `/requisitions/<id>/accountability/` — submit liquidation documents
- `/admin/` — administrative setup

## REST API

- `POST /api/auth/login/`, `POST /api/auth/logout/`, and `POST /api/auth/password-change/`
- `GET, POST /api/requisitions/`
- `GET, PUT, PATCH /api/requisitions/<id>/`
- `POST /api/requisitions/<id>/transition/` with `action` and optional `comments`
- `GET /api/requisition-logs/`
- `GET, POST /api/accountability/`
- `GET, POST /api/attachments/`
- `GET /api/files/<kind>/<id>/` for authenticated protected downloads
- `GET, POST /api/users/` (administrator only)

Session-authenticated browser/API requests must include Django's CSRF token for unsafe methods. HTTP Basic authentication is available for API clients and should only be used over TLS outside local development.
