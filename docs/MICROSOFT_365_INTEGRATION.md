# Microsoft 365 Integration Guide (Entra ID SSO, Teams App & Notifications)

This guide covers configuring and operating the Microsoft 365 integration for the **Financial Help Desk & Fund Requisition System (FinanceDesk)**.

---

## 1. Architecture Summary

- **Authoritative System**: Django remains the sole source of truth for requisitions, budget items, workflow transitions, protected evidence files, and immutable audit history (`RequisitionLog`).
- **Authentication**: OpenID Connect (OIDC) Authorization Code Flow with **PKCE (`S256`)** against Microsoft Entra ID v2.0 endpoints (`/auth/microsoft/login/` and `/auth/microsoft/callback/`).
- **Identity Linking**: Users are permanently linked via immutable `(entra_tenant_id, entra_object_id)` claims on `requisitions.User`. Existing enrolled local staff can be linked automatically on first SSO login via unambiguous email match (`MICROSOFT_LINK_EXISTING_BY_EMAIL=True`).
- **Role & Department Sync**: Entra Security Group Object IDs (or App Roles) map to FinanceDesk roles (`ADMIN`, `EXECUTIVE_DIRECTOR`, `FINANCE`, `INTERNAL_AUDIT`, `PROGRAM_LEAD`, `ME_OFFICER`, `SUPERVISOR`, `EMPLOYEE`) with deterministic privilege precedence.
- **Microsoft Teams**: Personal tabs (`Dashboard` and `Approval Queue`) load over HTTPS inside Teams with strict `Content-Security-Policy: frame-ancestors` protection (`TeamsFrameAncestorsMiddleware`) and a popup OIDC bridge (`/teams/tab/`, `/teams/auth/start/`, `/teams/auth/end/`).
- **Notifications**: Optional asynchronous Adaptive Card notifications via `BackgroundTask` (`send_microsoft_workflow_notification`) with retry backoff and cache-based deduplication.

---

## 2. Microsoft Entra ID App Registration

1. Sign in to the **Microsoft Entra admin center** (`https://entra.microsoft.com`) as a Cloud Application Administrator or Global Administrator.
2. Navigate to **Identity > Applications > App registrations > New registration**:
   - **Name**: `FinanceDesk Requisition System`
   - **Supported account types**: *Accounts in this organizational directory only (Single tenant)*
   - **Redirect URI**:
     - Platform: **Web**
     - URI: `https://<your-domain>/auth/microsoft/callback/`
3. Record the **Application (client) ID** (`MICROSOFT_CLIENT_ID`) and **Directory (tenant) ID** (`MICROSOFT_TENANT_ID`) from the **Overview** blade.
4. Under **Certificates & secrets > Client secrets > New client secret**:
   - Create a secret, set an expiration aligned with your rotation policy, and store the **Value** in `MICROSOFT_CLIENT_SECRET` (never commit secrets to Git).
5. Under **Token configuration > Add groups claim**:
   - Select **Groups assigned to the application** (recommended for organizations with >150 groups to avoid token overage without requiring Graph directory read permissions) or **Security groups**.
   - Keep **Group ID** selected for the ID token.
   - Optionally click **Add optional claim > ID > `email`, `preferred_username`, `given_name`, `family_name`**.
6. Under **Authentication**:
   - Set **Front-channel logout URL** (optional) or configure `MICROSOFT_POST_LOGOUT_REDIRECT_URI=https://<your-domain>/login/`.
   - Ensure **Implicit grant and hybrid flows** checkboxes (`Access tokens`, `ID tokens`) are **unchecked** (FinanceDesk uses Authorization Code Flow with PKCE).

---

## 3. Microsoft Graph Permissions & Admin Consent

FinanceDesk follows strict least-privilege design and requires **minimal delegated permissions**:

| Permission Name | Type | Admin Consent Required | Purpose & Data Accessed | When Required |
| :--- | :--- | :--- | :--- | :--- |
| `openid` | Delegated | No | OIDC sign-in and `sub`/`oid`/`tid` claims | Always (Entra SSO) |
| `profile` | Delegated | No | User display name (`name`, `given_name`, `family_name`) | Always (Entra SSO) |
| `email` | Delegated | No | User primary email (`email` / `preferred_username`) | Always (Entra SSO) |
| `GroupMember.Read.All` | Delegated | Yes | Reads `/v1.0/me/transitiveMemberOf` group IDs | **Optional** — only if `MICROSOFT_GRAPH_GROUP_FALLBACK=True` and users exceed 150–200 groups in the ID token |

> **Recommendation**: Use **Groups assigned to the application** in Entra Token Configuration so group IDs are emitted directly in the `id_token` without needing `GroupMember.Read.All`.

---

## 4. Role & Department Mapping Configuration

Configure `MICROSOFT_ROLE_MAPPING` as a JSON object mapping Entra Group Object IDs (UUIDs) or App Role values to FinanceDesk roles:

```env
MICROSOFT_ROLE_MAPPING={"11111111-1111-1111-1111-111111111111":"ADMIN","22222222-2222-2222-2222-222222222222":"EXECUTIVE_DIRECTOR","33333333-3333-3333-3333-333333333333":"FINANCE","44444444-4444-4444-4444-444444444444":"INTERNAL_AUDIT","55555555-5555-5555-5555-555555555555":"PROGRAM_LEAD","66666666-6666-6666-6666-666666666666":"ME_OFFICER","77777777-7777-7777-7777-777777777777":{"role":"SUPERVISOR","department":"Programs"},"88888888-8888-8888-8888-888888888888":"EMPLOYEE"}
```

- **Precedence**: If a user belongs to multiple mapped groups, FinanceDesk deterministically selects the highest-privilege role:
  `ADMIN > EXECUTIVE_DIRECTOR > FINANCE > INTERNAL_AUDIT > PROGRAM_LEAD > ME_OFFICER > SUPERVISOR > EMPLOYEE`
- **Admin Lockout Protection**: Existing `is_superuser` or `ADMIN` accounts are never downgraded automatically unless `MICROSOFT_STRICT_ROLE_SYNC=True`.
- **Unmapped Users (`MICROSOFT_UNMAPPED_USER_POLICY`)**:
  - `keep_existing` (development default): Preserves existing role for linked users; assigns `EMPLOYEE` for newly provisioned users. Avoid this policy in production because missing group claims can preserve stale access.
  - `assign_employee`: Assigns `EMPLOYEE` to non-admin users without a matching group.
  - `deny` (production default): Rejects sign-in (`403`) if the user has no matching group in `MICROSOFT_ROLE_MAPPING`.

In production, `MICROSOFT_STRICT_ROLE_SYNC=True` is also recommended. This prevents a stale administrator role from being silently preserved when role synchronization is authoritative. If administrator break-glass accounts must remain local, keep them unlinked from Entra ID and outside automatic role synchronization.

---

## 5. Microsoft Teams App Setup & Catalog Deployment

1. Enable Teams framing in your environment:
   ```env
   SITE_URL=https://financedesk.example.org
   MICROSOFT_TEAMS_ENABLED=True
   MICROSOFT_TEAMS_APP_ID=<new-uuid-for-teams-app>
   ```
2. In Entra ID (**Expose an API** blade of your App Registration):
   - Set **Application ID URI** to `api://<your-domain>/<MICROSOFT_CLIENT_ID>`.
   - Add authorized client applications for Microsoft Teams (`1fec8e78-bce4-4aaf-ab1b-5451cc387264` for Teams desktop/mobile and `5e3ce6c0-2b1f-4285-8d4b-75ee78787346` for Teams web).
3. Render and validate `manifest.json` using `requisitions.teams.build_teams_manifest()`:
   ```bash
   python manage.py shell -c "import json, pathlib; from requisitions.teams import build_teams_manifest; pathlib.Path('teams/manifest.json').write_text(json.dumps(build_teams_manifest(), indent=2))"
   ```
4. Create a ZIP package containing:
   - `manifest.json`
   - `color.png` (192x192 PNG icon)
   - `outline.png` (32x32 transparent PNG icon)
5. Upload the ZIP in the **Microsoft Teams Admin Center** (`https://admin.teams.microsoft.com` > **Teams apps > Manage apps > Upload new app**).

---

Teams framing is restricted to `/teams/` entry points. Normal application pages retain Django's clickjacking protection and must not be made broadly frameable.

## 6. User Migration & Rollback Procedures

### Phased User Migration
1. **Stage 1 (Dual Mode)**: Set `MICROSOFT_AUTH_ENABLED=True`, `MICROSOFT_ALLOW_LOCAL_LOGIN=True`, and `MICROSOFT_LINK_EXISTING_BY_EMAIL=True`. Existing staff click **"Sign in with Microsoft"**, which automatically links their `entra_object_id` and clears `must_change_password` while preserving their requisitions, approvals, and audit logs.
2. **Stage 2 (Cutover)**: Once all interactive staff have signed in via Microsoft, set `MICROSOFT_ALLOW_LOCAL_LOGIN=False` to require Microsoft Entra ID SSO on `/login/`. REST API clients and `/admin/` break-glass accounts remain functional as configured.

### Instant Rollback / Disabling Microsoft Integration
To disable Microsoft SSO, Teams framing, and Teams notifications immediately without database rollback or data loss:
```env
MICROSOFT_AUTH_ENABLED=False
MICROSOFT_ALLOW_LOCAL_LOGIN=True
MICROSOFT_TEAMS_ENABLED=False
MICROSOFT_NOTIFICATIONS_ENABLED=False
```
Restart the `web` and `worker` containers. All user records, requisitions, and audit logs remain intact, and `/login/` reverts to local username/password authentication.
