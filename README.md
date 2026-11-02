# Financial Help Desk & Fund Requisition System

A Django web application for submitting, reviewing, disbursing, and reconciling organizational fund requests. It includes a role-based dashboard, a five-stage approval workflow, an authenticated REST API, and an append-only application audit history.

## Workflow

1. **Employee** creates a draft for the planned activity, adds the justification and amount, and submits it.
2. **Supervisor** reviews departmental need and activity fit.
3. **M&E Officer** checks the request against indicators and compliance standards.
4. **Program Director** provides final program approval.
5. **Finance** reviews the request, creates the voucher, selects bank transfer or petty cash, and records disbursement.
   Administrative activities are the only exception: submission or resubmission routes directly to Finance; all non-administrative requests must pass through each preceding approval stage.
6. **Requester** submits actual spend, an activity report, and receipt or invoice evidence. Any unspent balance is calculated for reconciliation.
7. **Internal Audit** validates the accountability evidence before closing the request. The final request status is **Completed**.

At any approval stage, an approver can request a revision or reject the request. Revision comments and every status transition are recorded in the request history. Final accountability reconciles the approved amount to actual spending.

## Features

- Role-aware dashboard and approval queue for employees, supervisors, M&E, program directors, finance staff, Internal Audit, and administrators.
- Server-enforced status transitions, department-scoped supervisor approvals, and date-limited delegation.
- Accountability reports and receipt/invoice uploads, limited to PDF and common image formats, up to 15 MB per file.
- Read-only audit history and protected, authenticated file downloads.
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

To configure environment variables, copy `.env.example` to `.env` and export its values in your shell. For PostgreSQL, set `DB_ENGINE=django.db.backends.postgresql` and provide `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT`. Set a unique `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, and production `DJANGO_ALLOWED_HOSTS` before deployment. Do not use the example secret outside local development.

## Tests and checks

```bash
python manage.py check
python manage.py test
```

## Main web routes

- `/` — dashboard
- `/approval-queue/` — actions available to the signed-in user
- `/requisitions/new/` — create a draft
- `/requisitions/<id>/` — request details and audit history
- `/requisitions/<id>/accountability/` — submit liquidation documents
- `/admin/` — administrative setup

## REST API

- `POST /api/auth/login/` and `POST /api/auth/logout/`
- `GET, POST /api/requisitions/`
- `GET, PUT, PATCH /api/requisitions/<id>/`
- `POST /api/requisitions/<id>/transition/` with `action` and optional `comments`
- `GET /api/requisition-logs/`
- `GET, POST /api/accountability/`
- `GET, POST /api/attachments/`
- `GET /api/files/<kind>/<id>/` for authenticated protected downloads
- `GET, POST /api/users/` (administrator only)

Session-authenticated browser/API requests must include Django's CSRF token for unsafe methods. HTTP Basic authentication is available for API clients and should only be used over TLS outside local development.
