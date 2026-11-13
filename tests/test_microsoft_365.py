"""Tests for Microsoft Entra ID OIDC SSO, role synchronization, Teams integration, and notifications."""

from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from urllib.error import URLError
import zipfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from requisitions.entra_auth import (
    EntraAccessDeniedError,
    EntraAccountConflictError,
    EntraConfigurationError,
    EntraTokenValidationError,
    OIDC_SESSION_KEY,
    authenticate_or_provision_entra_user,
    build_pkce_pair,
    fetch_user_groups_from_graph,
    resolve_role_and_department,
    verify_id_token,
)
from requisitions.models import BackgroundTask, Requisition, RequisitionItem, User
from requisitions.tasks import execute_task_record
from requisitions.teams import build_teams_manifest, validate_teams_manifest
from requisitions.workflow import transition_requisition

TENANT_ID = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT_ID = "99999999-8888-7777-6666-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TEAMS_APP_ID = "12345678-1234-5678-1234-567812345678"

GROUP_ADMIN = "g-admin-0001"
GROUP_EXEC = "g-exec-0002"
GROUP_FINANCE = "g-finance-0003"
GROUP_AUDIT = "g-audit-0004"
GROUP_SUPERVISOR_PROGRAMS = "g-sup-prog-0005"
GROUP_EMPLOYEE = "g-emp-0006"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, byteorder="big"))


class RSAKeyFixture:
    """Helper for generating RSA keys, JWKS payloads, and signed OIDC ID tokens in tests."""

    def __init__(self, kid: str = "test-kid-1"):
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()

    def to_jwk(self) -> dict:
        numbers = self.public_key.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "kid": self.kid,
            "n": _int_to_b64url(numbers.n),
            "e": _int_to_b64url(numbers.e),
        }

    def sign_jwt(self, claims: dict, *, alg: str = "RS256", kid: str | None = None) -> str:
        header = {"typ": "JWT", "alg": alg, "kid": kid if kid is not None else self.kid}
        header_b64 = _b64url(json.dumps(header).encode("utf-8"))
        payload_b64 = _b64url(json.dumps(claims).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        if alg == "none":
            return f"{header_b64}.{payload_b64}."
        signature = self.private_key.sign(
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


RSA_FIXTURE = RSAKeyFixture()


def _base_claims(
    *,
    oid: str = "oid-user-100",
    tid: str = TENANT_ID,
    aud: str = CLIENT_ID,
    nonce: str = "nonce-123",
    email: str = "staff@example.org",
    name: str = "Grace Hopper",
    groups: list[str] | None = None,
    exp_offset: int = 3600,
) -> dict:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "aud": aud,
        "tid": tid,
        "oid": oid,
        "sub": f"sub-{oid}",
        "nonce": nonce,
        "iat": now - 10,
        "nbf": now - 10,
        "exp": now + exp_offset,
        "email": email,
        "preferred_username": email,
        "name": name,
    }
    if groups is not None:
        claims["groups"] = groups
    return claims


@override_settings(
    MICROSOFT_AUTH_ENABLED=True,
    MICROSOFT_ALLOW_LOCAL_LOGIN=True,
    MICROSOFT_TENANT_ID=TENANT_ID,
    MICROSOFT_CLIENT_ID=CLIENT_ID,
    MICROSOFT_CLIENT_SECRET="test-super-secret-value",
    MICROSOFT_REDIRECT_URI="http://testserver/auth/microsoft/callback/",
    MICROSOFT_ROLE_MAPPING={
        GROUP_ADMIN: User.ROLE_ADMIN,
        GROUP_EXEC: User.ROLE_EXECUTIVE_DIRECTOR,
        GROUP_FINANCE: User.ROLE_FINANCE,
        GROUP_AUDIT: User.ROLE_INTERNAL_AUDIT,
        GROUP_SUPERVISOR_PROGRAMS: {
            "role": User.ROLE_SUPERVISOR,
            "department": "Programs",
        },
        GROUP_EMPLOYEE: User.ROLE_EMPLOYEE,
    },
)
class EntraOIDCAndRoleSyncTests(TestCase):
    """Verify OIDC token validation, PKCE, account linking, and role synchronization."""

    def setUp(self):
        cache.clear()
        cache.set(f"fh:entra:jwks:{TENANT_ID}", [RSA_FIXTURE.to_jwk()], timeout=3600)

    def test_pkce_pair_follows_s256_spec(self):
        verifier, challenge = build_pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected)

    def test_verify_valid_id_token_and_reject_tampered_or_expired_tokens(self):
        claims = _base_claims(nonce="expected-nonce-1")
        token = RSA_FIXTURE.sign_jwt(claims)
        verified = verify_id_token(token, expected_nonce="expected-nonce-1")
        self.assertEqual(verified["oid"], "oid-user-100")

        # Nonce mismatch must be rejected
        with self.assertRaises(EntraTokenValidationError):
            verify_id_token(token, expected_nonce="wrong-nonce")

        # Expired token must be rejected
        expired_token = RSA_FIXTURE.sign_jwt(
            _base_claims(nonce="n1", exp_offset=-300)
        )
        with self.assertRaises(EntraTokenValidationError):
            verify_id_token(expired_token, expected_nonce="n1")

        # Wrong audience must be rejected
        wrong_aud_token = RSA_FIXTURE.sign_jwt(
            _base_claims(nonce="n1", aud="wrong-client-id")
        )
        with self.assertRaises(EntraTokenValidationError):
            verify_id_token(wrong_aud_token, expected_nonce="n1")

        # Wrong tenant must be rejected with EntraAccessDeniedError
        wrong_tid_token = RSA_FIXTURE.sign_jwt(
            _base_claims(nonce="n1", tid=OTHER_TENANT_ID)
        )
        with self.assertRaises(EntraAccessDeniedError):
            verify_id_token(wrong_tid_token, expected_nonce="n1")

        # Algorithm "none" downgrade attack must be rejected
        none_token = RSA_FIXTURE.sign_jwt(claims, alg="none")
        with self.assertRaises(EntraTokenValidationError):
            verify_id_token(none_token, expected_nonce="expected-nonce-1")

    @override_settings(MICROSOFT_TENANT_ID="common", MICROSOFT_ALLOWED_TENANTS=())
    def test_multi_tenant_alias_rejected_without_explicit_allowed_tenants(self):
        token = RSA_FIXTURE.sign_jwt(_base_claims(nonce="n1"))
        with self.assertRaises(EntraConfigurationError):
            verify_id_token(token, expected_nonce="n1")

    def test_jwks_key_rotation_refreshes_cache_automatically(self):
        rotated_key = RSAKeyFixture(kid="rotated-kid-2")
        token = rotated_key.sign_jwt(_base_claims(nonce="n-rot"))
        with patch(
            "requisitions.entra_auth._http_json_request",
            return_value={"keys": [rotated_key.to_jwk()]},
        ) as mock_http:
            verified = verify_id_token(token, expected_nonce="n-rot")
            self.assertEqual(verified["oid"], "oid-user-100")
            mock_http.assert_called_once()

    def test_existing_local_user_links_by_email_and_clears_must_change_password(self):
        local_user = User.objects.create_user(
            username="ghopper",
            email="staff@example.org",
            password="TempPassword!234",
            role=User.ROLE_FINANCE,
            department="Finance",
            must_change_password=True,
        )
        claims = _base_claims(oid="oid-grace-1", email="STAFF@example.org")
        result = authenticate_or_provision_entra_user(claims)

        self.assertEqual(result.user.pk, local_user.pk)
        self.assertTrue(result.linked_existing)
        self.assertFalse(result.created)
        local_user.refresh_from_db()
        self.assertEqual(local_user.entra_object_id, "oid-grace-1")
        self.assertEqual(local_user.entra_tenant_id, TENANT_ID)
        self.assertFalse(local_user.must_change_password)
        self.assertEqual(local_user.role, User.ROLE_FINANCE)

        # Subsequent login uses immutable oid even if email changes in Entra ID
        updated_claims = _base_claims(
            oid="oid-grace-1", email="grace.renamed@example.org"
        )
        second_result = authenticate_or_provision_entra_user(updated_claims)
        self.assertEqual(second_result.user.pk, local_user.pk)
        self.assertFalse(second_result.linked_existing)

    def test_email_conflict_with_already_linked_user_is_rejected(self):
        User.objects.create_user(
            username="existing_linked",
            email="conflict@example.org",
            password="Password!234",
            entra_object_id="oid-original-owner",
            entra_tenant_id=TENANT_ID,
        )
        claims = _base_claims(oid="oid-attacker", email="conflict@example.org")
        with self.assertRaises(EntraAccountConflictError):
            authenticate_or_provision_entra_user(claims)

    @override_settings(MICROSOFT_AUTO_PROVISION_USERS=False)
    def test_unlinked_user_rejected_when_auto_provision_disabled(self):
        claims = _base_claims(oid="oid-unknown-999", email="newperson@example.org")
        with self.assertRaises(EntraAccessDeniedError):
            authenticate_or_provision_entra_user(claims)

    @override_settings(MICROSOFT_AUTO_PROVISION_USERS=True)
    def test_auto_provision_creates_user_with_mapped_role_and_unusable_password(self):
        claims = _base_claims(
            oid="oid-new-sup",
            email="lead.prog@example.org",
            name="Alice Lead",
            groups=[GROUP_EMPLOYEE, GROUP_SUPERVISOR_PROGRAMS],
        )
        result = authenticate_or_provision_entra_user(claims)
        self.assertTrue(result.created)
        self.assertEqual(result.user.role, User.ROLE_SUPERVISOR)
        self.assertEqual(result.user.department, "Programs")
        self.assertFalse(result.user.has_usable_password())
        self.assertFalse(result.user.must_change_password)

    def test_disabled_user_cannot_authenticate_via_entra(self):
        User.objects.create_user(
            username="inactive_staff",
            email="inactive@example.org",
            password="Password!234",
            is_active=False,
            entra_object_id="oid-inactive-1",
            entra_tenant_id=TENANT_ID,
        )
        claims = _base_claims(oid="oid-inactive-1", email="inactive@example.org")
        with self.assertRaises(EntraAccessDeniedError):
            authenticate_or_provision_entra_user(claims)

    def test_role_precedence_and_admin_demotion_protection(self):
        # Multiple groups resolve to highest privilege (EXECUTIVE_DIRECTOR > FINANCE > EMPLOYEE)
        claims = _base_claims(
            groups=[GROUP_EMPLOYEE, GROUP_FINANCE, GROUP_EXEC]
        )
        role, _ = resolve_role_and_department(claims)
        self.assertEqual(role, User.ROLE_EXECUTIVE_DIRECTOR)

        # Existing ADMIN is protected from accidental demotion when matching a lower group
        admin_user = User.objects.create_user(
            username="sysadmin",
            email="admin@example.org",
            password="Password!234",
            role=User.ROLE_ADMIN,
        )
        low_claims = _base_claims(groups=[GROUP_EMPLOYEE])
        resolved_role, _ = resolve_role_and_department(
            low_claims, existing_user=admin_user
        )
        self.assertEqual(resolved_role, User.ROLE_ADMIN)

    @override_settings(MICROSOFT_ALLOWED_GROUPS=(GROUP_FINANCE, GROUP_ADMIN))
    def test_allowed_groups_gate_blocks_unauthorized_groups(self):
        claims = _base_claims(groups=[GROUP_EMPLOYEE])
        with self.assertRaises(EntraAccessDeniedError):
            resolve_role_and_department(claims)

    @override_settings(MICROSOFT_UNMAPPED_USER_POLICY="deny")
    def test_unmapped_user_policy_deny_rejects_users_without_role_group(self):
        claims = _base_claims(groups=["unrecognized-group-id"])
        with self.assertRaises(EntraAccessDeniedError):
            resolve_role_and_department(claims)

    @override_settings(MICROSOFT_GRAPH_GROUP_FALLBACK=True)
    def test_group_overage_fetches_groups_from_graph_when_enabled(self):
        claims = _base_claims(groups=None)
        claims["_claim_names"] = {"groups": "src1"}
        with patch(
            "requisitions.entra_auth._http_json_request",
            return_value={"value": [{"id": GROUP_AUDIT}]},
        ) as mock_graph:
            role, _ = resolve_role_and_department(
                claims, access_token="graph-access-token"
            )
            self.assertEqual(role, User.ROLE_INTERNAL_AUDIT)
            mock_graph.assert_called_once()

    @override_settings(MICROSOFT_GRAPH_GROUP_FALLBACK=True)
    def test_graph_group_overage_follows_pagination(self):
        next_url = "https://graph.microsoft.com/v1.0/me/transitiveMemberOf?$skiptoken=next"
        with patch(
            "requisitions.entra_auth._http_json_request",
            side_effect=[
                {
                    "value": [{"id": GROUP_EMPLOYEE}],
                    "@odata.nextLink": next_url,
                },
                {"value": [{"id": GROUP_FINANCE}]},
            ],
        ) as mock_graph:
            groups = fetch_user_groups_from_graph("graph-access-token")

        self.assertEqual(groups, sorted([GROUP_EMPLOYEE, GROUP_FINANCE]))
        self.assertEqual(mock_graph.call_count, 2)

    def test_end_to_end_browser_oidc_login_and_callback_flow(self):
        user = User.objects.create_user(
            username="finance_officer",
            email="finance@example.org",
            password="TempPassword!234",
            role=User.ROLE_FINANCE,
            must_change_password=True,
        )

        # 1. Start login flow
        start_resp = self.client.get(
            reverse("microsoft-login") + "?next=/approval-queue/"
        )
        self.assertEqual(start_resp.status_code, 302)
        self.assertIn(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize",
            start_resp["Location"],
        )
        self.assertIn("code_challenge_method=S256", start_resp["Location"])

        flow = self.client.session.get(OIDC_SESSION_KEY)
        self.assertIsNotNone(flow)

        # 2. Complete callback with valid signed ID token
        signed_id_token = RSA_FIXTURE.sign_jwt(
            _base_claims(
                oid="oid-fin-01",
                email="finance@example.org",
                nonce=flow["nonce"],
                groups=[GROUP_FINANCE],
            )
        )
        with patch(
            "requisitions.views.exchange_code_for_tokens",
            return_value={"id_token": signed_id_token, "access_token": "tok"},
        ):
            cb_resp = self.client.get(
                reverse("microsoft-callback"),
                {"code": "auth-code-123", "state": flow["state"]},
            )

        self.assertEqual(cb_resp.status_code, 302)
        self.assertEqual(cb_resp["Location"], "/approval-queue/")
        user.refresh_from_db()
        self.assertEqual(user.entra_object_id, "oid-fin-01")
        self.assertFalse(user.must_change_password)

    @override_settings(MICROSOFT_ALLOW_LOCAL_LOGIN=False)
    def test_local_password_login_blocked_when_disabled(self):
        User.objects.create_user(
            username="localuser",
            password="ValidPassword!234",
            role=User.ROLE_EMPLOYEE,
        )
        resp = self.client.post(
            reverse("login"),
            {"username": "localuser", "password": "ValidPassword!234"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertContains(
            resp,
            "Local password sign-in is disabled. Please sign in with Microsoft.",
            status_code=403,
        )

    @override_settings(LOGIN_RATE_LIMIT=1, LOGIN_RATE_WINDOW_SECONDS=300)
    def test_failed_local_login_is_throttled(self):
        first = self.client.post(
            reverse("login"),
            {"username": "unknown-user", "password": "wrong-password"},
        )
        second = self.client.post(
            reverse("login"),
            {"username": "unknown-user", "password": "wrong-password"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)


class TeamsAndNotificationsIntegrationTests(TestCase):
    """Verify Teams CSP framing, tab views, manifest validation, and async notifications."""

    def setUp(self):
        cache.clear()
        self.employee = User.objects.create_user(
            username="req_emp",
            email="emp@example.org",
            password="Password!234",
            role=User.ROLE_EMPLOYEE,
            department="Programs",
        )
        self.supervisor = User.objects.create_user(
            username="req_sup",
            email="sup@example.org",
            password="Password!234",
            role=User.ROLE_SUPERVISOR,
            department="Programs",
        )

    @override_settings(MICROSOFT_TEAMS_ENABLED=True)
    def test_teams_frame_ancestors_header_applied_when_enabled(self):
        resp = self.client.get(reverse("teams-tab"))
        self.assertEqual(resp.status_code, 200)
        csp = resp.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'self' https://teams.microsoft.com", csp)
        self.assertNotIn("X-Frame-Options", resp)

    @override_settings(MICROSOFT_TEAMS_ENABLED=True)
    def test_normal_pages_retain_clickjacking_protection_when_teams_is_enabled(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("frame-ancestors", resp.get("Content-Security-Policy", ""))
        self.assertEqual(resp.get("X-Frame-Options"), "DENY")

    @override_settings(
        MICROSOFT_AUTH_ENABLED=True,
        MICROSOFT_TENANT_ID=TENANT_ID,
        MICROSOFT_CLIENT_ID=CLIENT_ID,
        MICROSOFT_CLIENT_SECRET="test-super-secret-value",
        MICROSOFT_POST_LOGOUT_REDIRECT_URI="https://financedesk.example.org/login/",
    )
    def test_microsoft_logout_ends_local_and_provider_sessions(self):
        user = User.objects.create_user(
            username="entra_user",
            email="entra@example.org",
            password="Password!234",
            role=User.ROLE_EMPLOYEE,
            entra_object_id="oid-logout-1",
            entra_tenant_id=TENANT_ID,
        )
        self.client.force_login(user)

        response = self.client.post(reverse("logout"), {"microsoft_logout": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/logout",
            response["Location"],
        )
        self.assertIn(
            "post_logout_redirect_uri=https%3A%2F%2Ffinancedesk.example.org%2Flogin%2F",
            response["Location"],
        )
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_teams_manifest_generation_and_validation(self):
        manifest = build_teams_manifest(
            app_id=TEAMS_APP_ID,
            client_id=CLIENT_ID,
            site_url="https://financedesk.example.org",
        )
        self.assertEqual(manifest["id"], TEAMS_APP_ID)
        self.assertEqual(manifest["webApplicationInfo"]["id"], CLIENT_ID)
        self.assertIn("financedesk.example.org", manifest["validDomains"])

        # Non-HTTPS URLs must be rejected for production Teams manifests
        with self.assertRaises(ValidationError):
            build_teams_manifest(
                app_id=TEAMS_APP_ID,
                client_id=CLIENT_ID,
                site_url="http://financedesk.example.org",
                require_https=True,
            )

        # Invalid UUIDs must be rejected
        manifest["id"] = "not-a-uuid"
        with self.assertRaises(ValidationError):
            validate_teams_manifest(manifest)

    @override_settings(
        MICROSOFT_NOTIFICATIONS_ENABLED=True,
        MICROSOFT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/test-hook",
        BACKGROUND_TASK_MODE="eager",
    )
    def test_async_microsoft_notification_delivery_deduplication_and_retry(self):
        req = Requisition.objects.create(
            requestor=self.employee,
            title="Field Survey Transport",
            description="Transport for field enumerators",
            indicator="IND-101",
            amount=Decimal("250000.00"),
            department="Programs",
        )
        RequisitionItem.objects.create(
            requisition=req,
            item_description="Fuel",
            quantity=Decimal("1"),
            unit_cost=Decimal("250000.00"),
        )

        class DummyHTTPResponse(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with patch(
            "requisitions.tasks.urlopen",
            return_value=DummyHTTPResponse(b"1"),
        ) as mock_urlopen:
            transition_requisition(req.pk, self.employee, "SUBMIT")
            self.assertEqual(mock_urlopen.call_count, 1)

            task = BackgroundTask.objects.filter(
                task_name="send_microsoft_workflow_notification"
            ).first()
            self.assertIsNotNone(task)
            self.assertEqual(task.status, BackgroundTask.STATUS_COMPLETED)

            # Re-executing the same task is deduplicated and does not send a second webhook
            execute_task_record(task)
            self.assertEqual(mock_urlopen.call_count, 1)

    @override_settings(
        MICROSOFT_NOTIFICATIONS_ENABLED=True,
        MICROSOFT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/test-hook",
        BACKGROUND_TASK_MODE="eager",
    )
    def test_webhook_failure_does_not_block_workflow_transition_and_schedules_retry(self):
        req = Requisition.objects.create(
            requestor=self.employee,
            title="Workshop Catering",
            description="Catering for community workshop",
            indicator="IND-202",
            amount=Decimal("150000.00"),
            department="Programs",
        )
        RequisitionItem.objects.create(
            requisition=req,
            item_description="Refreshments",
            quantity=Decimal("1"),
            unit_cost=Decimal("150000.00"),
        )

        with patch(
            "requisitions.tasks.urlopen",
            side_effect=URLError("Connection timed out"),
        ):
            updated = transition_requisition(req.pk, self.employee, "SUBMIT")

        # Requisition still transitions cleanly even when webhook is unreachable
        self.assertEqual(updated.status, Requisition.STATUS_PENDING_SUPERVISOR)
        task = BackgroundTask.objects.filter(
            task_name="send_microsoft_workflow_notification"
        ).first()
        self.assertIsNotNone(task)
        self.assertEqual(task.status, BackgroundTask.STATUS_PENDING)
        self.assertEqual(task.attempts, 1)
        self.assertIn("Microsoft Teams notification delivery failed", task.last_error)

    def test_build_teams_package_command_creates_valid_zip_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "financedesk-teams.zip"
            call_command(
                "build_teams_package",
                output=str(zip_path),
                app_id=TEAMS_APP_ID,
                client_id=CLIENT_ID,
                site_url="https://financedesk.example.org",
            )
            self.assertTrue(zip_path.exists())
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = set(zf.namelist())
                self.assertEqual(names, {"manifest.json", "color.png", "outline.png"})
                manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertEqual(manifest_data["id"], TEAMS_APP_ID)
                self.assertTrue(zf.read("color.png").startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertTrue(zf.read("outline.png").startswith(b"\x89PNG\r\n\x1a\n"))

    @override_settings(
        WORKFLOW_EMAIL_NOTIFICATIONS=True,
        BACKGROUND_TASK_MODE="eager",
    )
    def test_workflow_notification_emails_requester_and_next_stage_approvers_and_delegates(self):
        today = timezone.localdate()
        delegate_user = User.objects.create_user(
            username="sup_delegate",
            email="delegate@example.org",
            password="Password!234",
            role=User.ROLE_EMPLOYEE,
            department="Programs",
        )
        self.supervisor.delegate = delegate_user
        self.supervisor.delegate_start_date = today - timedelta(days=1)
        self.supervisor.delegate_end_date = today + timedelta(days=5)
        self.supervisor.save()

        req = Requisition.objects.create(
            requestor=self.employee,
            title="Community Outreach",
            description="Outreach transport and materials",
            indicator="IND-303",
            amount=Decimal("300000.00"),
            department="Programs",
        )
        RequisitionItem.objects.create(
            requisition=req,
            item_description="Materials",
            quantity=Decimal("1"),
            unit_cost=Decimal("300000.00"),
        )

        mail.outbox.clear()
        transition_requisition(req.pk, self.employee, "SUBMIT")

        # 1 email to requester + 1 action-required email to supervisor & active delegate
        self.assertEqual(len(mail.outbox), 2)
        requester_email = mail.outbox[0]
        reviewer_email = mail.outbox[1]
        self.assertEqual(requester_email.to, ["emp@example.org"])
        self.assertEqual(
            sorted(reviewer_email.to),
            ["delegate@example.org", "sup@example.org"],
        )
        self.assertIn("Action Required", reviewer_email.subject)

    def test_prune_tasks_command_deletes_only_old_completed_and_failed_tasks(self):
        old_time = timezone.now() - timedelta(days=45)
        old_completed = BackgroundTask.objects.create(
            task_name="send_workflow_notification",
            status=BackgroundTask.STATUS_COMPLETED,
            completed_at=old_time,
        )
        recent_completed = BackgroundTask.objects.create(
            task_name="send_workflow_notification",
            status=BackgroundTask.STATUS_COMPLETED,
            completed_at=timezone.now() - timedelta(days=2),
        )
        pending_task = BackgroundTask.objects.create(
            task_name="send_workflow_notification",
            status=BackgroundTask.STATUS_PENDING,
        )

        call_command("prune_tasks", days=30)

        self.assertFalse(BackgroundTask.objects.filter(pk=old_completed.pk).exists())
        self.assertTrue(BackgroundTask.objects.filter(pk=recent_completed.pk).exists())
        self.assertTrue(BackgroundTask.objects.filter(pk=pending_task.pk).exists())
