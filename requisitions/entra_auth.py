"""Microsoft Entra ID (Azure AD) OpenID Connect authentication, PKCE, and role synchronization."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import logging
import secrets
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from requisitions.models import User

logger = logging.getLogger("requisitions.entra")

OIDC_SESSION_KEY = "entra_oidc_flow"
OIDC_STATE_MAX_AGE_SECONDS = 600
JWKS_CACHE_TIMEOUT_SECONDS = 3600
MULTI_TENANT_ALIASES = {"common", "organizations", "consumers"}

# Deterministic highest-to-lowest privilege precedence when a user belongs to multiple Entra groups.
ROLE_PRECEDENCE: dict[str, int] = {
    User.ROLE_ADMIN: 80,
    User.ROLE_EXECUTIVE_DIRECTOR: 70,
    User.ROLE_FINANCE: 60,
    User.ROLE_INTERNAL_AUDIT: 50,
    User.ROLE_PROGRAM_LEAD: 40,
    User.ROLE_ME_OFFICER: 30,
    User.ROLE_SUPERVISOR: 20,
    User.ROLE_EMPLOYEE: 10,
}


class EntraAuthError(Exception):
    """Base exception for Microsoft Entra ID authentication failures."""

    user_message = "Microsoft sign-in failed. Please try again or contact an administrator."


class EntraConfigurationError(EntraAuthError):
    """Raised when Entra ID settings are incomplete or invalid."""

    user_message = "Microsoft sign-in is not configured properly."


class EntraTokenValidationError(EntraAuthError):
    """Raised when an OIDC state, nonce, signature, or claim check fails."""

    user_message = "Could not verify your Microsoft sign-in token. Please try signing in again."


class EntraAccessDeniedError(EntraAuthError):
    """Raised when a user's tenant, group membership, or account status is not permitted."""

    def __init__(self, message: str, *, user_message: str | None = None):
        super().__init__(message)
        if user_message:
            self.user_message = user_message
        else:
            self.user_message = message


class EntraAccountConflictError(EntraAccessDeniedError):
    """Raised when an email matches multiple local accounts or an already-linked account."""


@dataclass(frozen=True)
class EntraSyncResult:
    """Summary of an authenticated and synchronized Entra ID user."""

    user: User
    created: bool
    linked_existing: bool
    role_updated: bool


def is_microsoft_auth_enabled() -> bool:
    """Return whether Microsoft Entra ID SSO is enabled and minimally configured."""
    return bool(
        getattr(settings, "MICROSOFT_AUTH_ENABLED", False)
        and getattr(settings, "MICROSOFT_TENANT_ID", "").strip()
        and getattr(settings, "MICROSOFT_CLIENT_ID", "").strip()
    )


def _validate_tenant_configuration() -> str:
    """Ensure a specific tenant ID is configured and multi-tenant aliases are blocked unless allowed."""
    if not getattr(settings, "MICROSOFT_AUTH_ENABLED", False):
        raise EntraConfigurationError("MICROSOFT_AUTH_ENABLED is False.")

    tenant_id = getattr(settings, "MICROSOFT_TENANT_ID", "").strip()
    client_id = getattr(settings, "MICROSOFT_CLIENT_ID", "").strip()
    if not tenant_id or not client_id:
        raise EntraConfigurationError(
            "Both MICROSOFT_TENANT_ID and MICROSOFT_CLIENT_ID must be configured."
        )

    allowed_tenants = set(getattr(settings, "MICROSOFT_ALLOWED_TENANTS", ()))
    if tenant_id.lower() in MULTI_TENANT_ALIASES and not allowed_tenants:
        raise EntraConfigurationError(
            "Multi-tenant authority aliases ('common', 'organizations', 'consumers') require "
            "explicit MICROSOFT_ALLOWED_TENANTS to enforce tenant isolation."
        )
    return tenant_id


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding_len = (4 - len(segment) % 4) % 4
    return base64.urlsafe_b64decode((segment + ("=" * padding_len)).encode("ascii"))


def build_pkce_pair() -> tuple[str, str]:
    """Generate a high-entropy RFC 7636 PKCE code_verifier and S256 code_challenge."""
    code_verifier = _b64url_encode(secrets.token_bytes(48))
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = _b64url_encode(digest)
    return code_verifier, code_challenge


def resolve_redirect_uri(request) -> str:
    """Return the configured MICROSOFT_REDIRECT_URI or build the callback URL from the request."""
    configured = getattr(settings, "MICROSOFT_REDIRECT_URI", "").strip()
    if configured:
        return configured
    callback_path = reverse("microsoft-callback")
    return request.build_absolute_uri(callback_path)


def build_authorization_request(
    request,
    *,
    next_url: str = "",
    teams_popup: bool = False,
    login_hint: str = "",
) -> str:
    """Initialize OIDC state, nonce, and PKCE in the session and return the Entra authorize URL."""
    tenant_id = _validate_tenant_configuration()
    client_id = getattr(settings, "MICROSOFT_CLIENT_ID", "").strip()
    redirect_uri = resolve_redirect_uri(request)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier, code_challenge = build_pkce_pair()

    request.session[OIDC_SESSION_KEY] = {
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "next_url": next_url or "",
        "teams_popup": bool(teams_popup),
        "created_at": int(time.time()),
    }
    request.session.modified = True

    scopes = list(
        getattr(settings, "MICROSOFT_OIDC_SCOPES", ("openid", "profile", "email"))
    )
    graph_group_scope = "https://graph.microsoft.com/GroupMember.Read.All"
    if getattr(settings, "MICROSOFT_GRAPH_GROUP_FALLBACK", False) and graph_group_scope not in scopes:
        scopes.append(graph_group_scope)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": getattr(settings, "MICROSOFT_AUTH_PROMPT", "select_account"),
    }
    if login_hint:
        params["login_hint"] = login_hint

    authority_host = getattr(
        settings, "MICROSOFT_AUTHORITY_HOST", "https://login.microsoftonline.com"
    ).rstrip("/")
    return f"{authority_host}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(params)}"


def build_logout_url(request) -> str:
    """Build the Entra logout URL with a safe local post-logout redirect."""
    tenant_id = _validate_tenant_configuration()
    authority_host = getattr(
        settings, "MICROSOFT_AUTHORITY_HOST", "https://login.microsoftonline.com"
    ).rstrip("/")
    configured_redirect = getattr(
        settings, "MICROSOFT_POST_LOGOUT_REDIRECT_URI", ""
    ).strip()
    redirect_uri = configured_redirect or request.build_absolute_uri(reverse("login"))
    return (
        f"{authority_host}/{tenant_id}/oauth2/v2.0/logout?"
        f"{urlencode({'post_logout_redirect_uri': redirect_uri})}"
    )


def _http_json_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform an HTTPS JSON request without logging sensitive payloads or tokens."""
    timeout = int(getattr(settings, "MICROSOFT_HTTP_TIMEOUT", 10))
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    req = Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except HTTPError as exc:
        error_code = f"HTTP {exc.code}"
        try:
            err_payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(err_payload, dict) and err_payload.get("error"):
                error_code = f"{error_code} ({err_payload.get('error')})"
        except Exception:
            pass
        logger.warning("Microsoft endpoint request failed: %s", error_code)
        raise EntraTokenValidationError(
            f"Microsoft token endpoint returned {error_code}."
        ) from exc
    except (URLError, TimeoutError, ValueError) as exc:
        logger.warning("Microsoft endpoint communication error: %s", exc.__class__.__name__)
        raise EntraTokenValidationError(
            "Unable to reach Microsoft identity endpoints."
        ) from exc


def exchange_code_for_tokens(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange an authorization code and PKCE verifier for Microsoft OIDC tokens."""
    tenant_id = _validate_tenant_configuration()
    client_id = getattr(settings, "MICROSOFT_CLIENT_ID", "").strip()
    client_secret = getattr(settings, "MICROSOFT_CLIENT_SECRET", "").strip()
    if not client_secret:
        raise EntraConfigurationError(
            "MICROSOFT_CLIENT_SECRET must be set to exchange authorization codes."
        )

    authority_host = getattr(
        settings, "MICROSOFT_AUTHORITY_HOST", "https://login.microsoftonline.com"
    ).rstrip("/")
    token_url = f"{authority_host}/{tenant_id}/oauth2/v2.0/token"

    form_data = urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")

    return _http_json_request(
        token_url,
        method="POST",
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _fetch_jwks_keys(tenant_id: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Fetch and cache Microsoft's public signing keys (JWKS) for the configured tenant."""
    cache_key = f"fh:entra:jwks:{tenant_id}"
    if not force_refresh:
        cached = cache.get(cache_key)
        if isinstance(cached, list) and cached:
            return cached

    authority_host = getattr(
        settings, "MICROSOFT_AUTHORITY_HOST", "https://login.microsoftonline.com"
    ).rstrip("/")
    jwks_url = f"{authority_host}/{tenant_id}/discovery/v2.0/keys"
    payload = _http_json_request(jwks_url, method="GET")
    keys = payload.get("keys")
    if not isinstance(keys, list) or not keys:
        raise EntraTokenValidationError("Microsoft JWKS response did not contain signing keys.")
    cache.set(cache_key, keys, timeout=JWKS_CACHE_TIMEOUT_SECONDS)
    return keys


def _jwk_to_rsa_public_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    """Construct an RSAPublicKey from a standard JWK modulus (n) and exponent (e)."""
    if jwk.get("kty") != "RSA" or not jwk.get("n") or not jwk.get("e"):
        raise EntraTokenValidationError("Unsupported JWK key type or missing RSA parameters.")
    n_int = int.from_bytes(_b64url_decode(jwk["n"]), byteorder="big")
    e_int = int.from_bytes(_b64url_decode(jwk["e"]), byteorder="big")
    return rsa.RSAPublicNumbers(e_int, n_int).public_key()


def _verify_jwt_rs256_signature(
    id_token: str,
    tenant_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify the RS256 cryptographic signature of a JWT against Microsoft's JWKS keys."""
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        raise EntraTokenValidationError("Malformed ID token structure.")

    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment).decode("utf-8"))
        claims = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
        signature = _b64url_decode(signature_segment)
    except Exception as exc:
        raise EntraTokenValidationError("Malformed ID token encoding.") from exc

    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise EntraTokenValidationError("Invalid ID token JSON structure.")

    alg = header.get("alg")
    if alg != "RS256":
        raise EntraTokenValidationError(f"Unsupported ID token signing algorithm: {alg!r}.")

    kid = header.get("kid")
    if not kid:
        raise EntraTokenValidationError("ID token header is missing key ID (kid).")

    keys = _fetch_jwks_keys(tenant_id, force_refresh=False)
    matching_jwk = next((k for k in keys if k.get("kid") == kid), None)
    if matching_jwk is None:
        # Refresh once in case Microsoft rotated signing keys recently.
        keys = _fetch_jwks_keys(tenant_id, force_refresh=True)
        matching_jwk = next((k for k in keys if k.get("kid") == kid), None)
    if matching_jwk is None:
        raise EntraTokenValidationError("No matching Microsoft JWKS public key found for token kid.")

    public_key = _jwk_to_rsa_public_key(matching_jwk)
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    try:
        public_key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise EntraTokenValidationError("ID token cryptographic signature verification failed.") from exc

    return header, claims


def verify_id_token(
    id_token: str,
    *,
    expected_nonce: str,
) -> dict[str, Any]:
    """Validate ID token signature, issuer, audience, tenant, nonce, and time claims."""
    configured_tenant = _validate_tenant_configuration()
    client_id = getattr(settings, "MICROSOFT_CLIENT_ID", "").strip()
    _, claims = _verify_jwt_rs256_signature(id_token, configured_tenant)

    now = int(time.time())
    clock_skew = int(getattr(settings, "MICROSOFT_TOKEN_CLOCK_SKEW_SECONDS", 60))

    # 1. Validate expiry (exp), not-before (nbf), and issued-at (iat)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now >= (int(exp) + clock_skew):
        raise EntraTokenValidationError("ID token has expired.")

    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and (now + clock_skew) < int(nbf):
        raise EntraTokenValidationError("ID token is not yet valid (nbf).")

    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and (now + clock_skew) < int(iat):
        raise EntraTokenValidationError("ID token issued-at timestamp is in the future.")

    # 2. Validate tenant ID (tid) isolation
    token_tid = str(claims.get("tid") or "").strip()
    if not token_tid:
        raise EntraTokenValidationError("ID token is missing tenant ID (tid) claim.")

    allowed_tenants = {
        t.lower() for t in getattr(settings, "MICROSOFT_ALLOWED_TENANTS", ()) if t
    }
    if configured_tenant.lower() not in MULTI_TENANT_ALIASES:
        allowed_tenants.add(configured_tenant.lower())
    if token_tid.lower() not in allowed_tenants:
        logger.warning("Rejected Entra token from unauthorized tenant tid=%s", token_tid)
        raise EntraAccessDeniedError(
            f"Tenant {token_tid} is not authorized to access this application.",
            user_message="Your organization tenant is not authorized to access FinanceDesk.",
        )

    # 3. Validate issuer (iss)
    authority_host = getattr(
        settings, "MICROSOFT_AUTHORITY_HOST", "https://login.microsoftonline.com"
    ).rstrip("/")
    expected_issuers = {
        f"{authority_host}/{token_tid}/v2.0",
        f"https://sts.windows.net/{token_tid}/",
    }
    token_iss = str(claims.get("iss") or "").strip()
    if token_iss not in expected_issuers:
        raise EntraTokenValidationError(f"Unexpected ID token issuer: {token_iss!r}.")

    # 4. Validate audience (aud)
    token_aud = claims.get("aud")
    if isinstance(token_aud, list):
        aud_valid = client_id in token_aud
    else:
        aud_valid = str(token_aud or "") == client_id
    if not aud_valid:
        raise EntraTokenValidationError("ID token audience (aud) does not match MICROSOFT_CLIENT_ID.")

    # 5. Validate nonce
    token_nonce = str(claims.get("nonce") or "")
    if not expected_nonce or not token_nonce or not secrets.compare_digest(token_nonce, expected_nonce):
        raise EntraTokenValidationError("ID token nonce mismatch.")

    # 6. Validate stable Entra Object ID (oid)
    oid = str(claims.get("oid") or "").strip()
    if not oid:
        raise EntraTokenValidationError("ID token is missing immutable object ID (oid) claim.")

    return claims


def fetch_user_groups_from_graph(access_token: str) -> list[str]:
    """Fetch transitive group Object IDs from Microsoft Graph when ID token group overage occurs."""
    if not access_token:
        return []
    graph_url = (
        "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/"
        "microsoft.graph.group?$select=id&$top=200"
    )
    group_ids: set[str] = set()
    page_count = 0
    try:
        while graph_url and page_count < 20:
            if not graph_url.startswith("https://graph.microsoft.com/"):
                raise EntraTokenValidationError("Microsoft Graph returned an unsafe pagination URL.")
            payload = _http_json_request(
                graph_url,
                method="GET",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            values = payload.get("value") or []
            group_ids.update(
                str(item["id"]).strip()
                for item in values
                if isinstance(item, dict) and item.get("id")
            )
            graph_url = str(payload.get("@odata.nextLink") or "").strip()
            page_count += 1
    except EntraAuthError as exc:
        logger.warning("Failed to resolve group overage from Microsoft Graph: %s", exc)
        return []
    if graph_url:
        logger.warning("Microsoft Graph group pagination exceeded the configured page limit.")
    return sorted(group_ids)


def extract_claims_groups_and_roles(
    claims: dict[str, Any],
    *,
    access_token: str = "",
) -> set[str]:
    """Extract Entra group Object IDs and App Role values from verified token claims."""
    principals: set[str] = set()
    for claim_key in ("groups", "roles"):
        raw_values = claims.get(claim_key)
        if isinstance(raw_values, list):
            for val in raw_values:
                if val:
                    principals.add(str(val).strip())

    # Detect Entra ID group overage (_claim_names.groups)
    claim_names = claims.get("_claim_names")
    if isinstance(claim_names, dict) and "groups" in claim_names:
        if getattr(settings, "MICROSOFT_GRAPH_GROUP_FALLBACK", False) and access_token:
            principals.update(fetch_user_groups_from_graph(access_token))
        else:
            logger.warning(
                "Entra ID token indicates group overage for oid=%s. Configure 'Groups assigned to "
                "the application' in Entra ID or enable MICROSOFT_GRAPH_GROUP_FALLBACK.",
                claims.get("oid", "unknown"),
            )
    return principals


def _normalize_role_code(role_value: str) -> str:
    """Validate and normalize a target role string against User.ROLE_CHOICES."""
    normalized = (role_value or "").strip().upper()
    valid_codes = dict(User.ROLE_CHOICES)
    if normalized in valid_codes:
        return normalized
    label_to_code = {label.upper(): code for code, label in User.ROLE_CHOICES}
    if normalized in label_to_code:
        return label_to_code[normalized]
    raise EntraConfigurationError(f"Invalid role '{role_value}' in MICROSOFT_ROLE_MAPPING.")


def resolve_role_and_department(
    claims: dict[str, Any],
    *,
    existing_user: User | None = None,
    access_token: str = "",
) -> tuple[str | None, str | None]:
    """Map Entra group/app-role claims to a FinanceDesk role and optional department.

    Enforces:
    - MICROSOFT_ALLOWED_GROUPS access gate if configured
    - Deterministic highest-privilege precedence when multiple groups match
    - Unmapped user policy (keep_existing, assign_employee, or deny)
    - Protection against accidental administrator demotion
    """
    user_principals = extract_claims_groups_and_roles(claims, access_token=access_token)

    allowed_groups = {
        g.strip() for g in getattr(settings, "MICROSOFT_ALLOWED_GROUPS", ()) if g and g.strip()
    }
    if allowed_groups and not (user_principals & allowed_groups):
        logger.warning(
            "Denied Entra login for oid=%s: user is not a member of MICROSOFT_ALLOWED_GROUPS.",
            claims.get("oid", "unknown"),
        )
        raise EntraAccessDeniedError(
            "Your Microsoft account does not belong to an authorized security group for FinanceDesk."
        )

    role_mapping: dict[str, Any] = getattr(settings, "MICROSOFT_ROLE_MAPPING", {}) or {}
    matched_candidates: list[tuple[int, str, str | None]] = []

    for group_key, target_spec in role_mapping.items():
        if str(group_key).strip() not in user_principals:
            continue
        if isinstance(target_spec, dict):
            role_code = _normalize_role_code(str(target_spec.get("role", "")))
            mapped_dept = str(target_spec.get("department", "")).strip() or None
        else:
            role_code = _normalize_role_code(str(target_spec))
            mapped_dept = None
        precedence = ROLE_PRECEDENCE.get(role_code, 0)
        matched_candidates.append((precedence, role_code, mapped_dept))

    # Extract optional department claim from token if present
    dept_claim_name = getattr(settings, "MICROSOFT_DEPARTMENT_CLAIM", "department") or "department"
    claim_department = str(claims.get(dept_claim_name) or "").strip() or None

    if matched_candidates:
        # Sort by highest privilege precedence first
        matched_candidates.sort(key=lambda item: item[0], reverse=True)
        _, best_role, mapped_department = matched_candidates[0]
        resolved_department = mapped_department or claim_department
        if (
            existing_user is not None
            and (existing_user.is_superuser or existing_user.role == User.ROLE_ADMIN)
            and best_role != User.ROLE_ADMIN
            and not getattr(settings, "MICROSOFT_STRICT_ROLE_SYNC", False)
        ):
            # Protect existing administrators from accidental demotion unless strict sync is forced
            best_role = User.ROLE_ADMIN
        return best_role, resolved_department

    # No matching group in MICROSOFT_ROLE_MAPPING
    unmapped_policy = (
        getattr(settings, "MICROSOFT_UNMAPPED_USER_POLICY", "keep_existing")
        .strip()
        .lower()
    )
    if unmapped_policy == "deny":
        logger.warning(
            "Denied Entra login for oid=%s: no matching role group and MICROSOFT_UNMAPPED_USER_POLICY=deny.",
            claims.get("oid", "unknown"),
        )
        raise EntraAccessDeniedError(
            "Your Microsoft account has not been assigned a FinanceDesk role."
        )
    if unmapped_policy == "assign_employee":
        if (
            existing_user is not None
            and (existing_user.is_superuser or existing_user.role == User.ROLE_ADMIN)
            and not getattr(settings, "MICROSOFT_STRICT_ROLE_SYNC", False)
        ):
            return existing_user.role, claim_department
        return User.ROLE_EMPLOYEE, claim_department

    # Default "keep_existing": preserve existing user's role, or default new users to EMPLOYEE
    if existing_user is not None:
        return existing_user.role, claim_department
    return User.ROLE_EMPLOYEE, claim_department


def _extract_email_and_names(claims: dict[str, Any]) -> tuple[str, str, str]:
    """Extract normalized email/UPN and first/last names from OIDC claims."""
    raw_email = str(
        claims.get("email") or claims.get("preferred_username") or claims.get("upn") or ""
    ).strip()
    email = raw_email.lower() if "@" in raw_email else ""

    given_name = str(claims.get("given_name") or "").strip()
    family_name = str(claims.get("family_name") or "").strip()
    display_name = str(claims.get("name") or "").strip()

    if not given_name and not family_name and display_name:
        parts = display_name.split(None, 1)
        given_name = parts[0]
        family_name = parts[1] if len(parts) > 1 else ""

    return email, given_name[:150], family_name[:150]


def _generate_unique_username(email: str, oid: str) -> str:
    """Generate a collision-free username for an auto-provisioned Entra user."""
    base = email.split("@", 1)[0] if email else f"entra_{oid[:12]}"
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in "._-")[:120] or f"user_{oid[:8]}"
    candidate = cleaned
    suffix = 1
    while User.objects.filter(username__iexact=candidate).exists():
        suffix += 1
        candidate = f"{cleaned[:110]}_{suffix}"
    return candidate


@transaction.atomic
def authenticate_or_provision_entra_user(
    claims: dict[str, Any],
    *,
    access_token: str = "",
) -> EntraSyncResult:
    """Link, update, or auto-provision a local User from verified Entra ID claims."""
    oid = str(claims["oid"]).strip()
    tid = str(claims["tid"]).strip()
    email, first_name, last_name = _extract_email_and_names(claims)

    user = User.objects.select_for_update().filter(entra_object_id=oid).first()
    created = False
    linked_existing = False

    if user is not None:
        if user.entra_tenant_id and user.entra_tenant_id.lower() != tid.lower():
            logger.error(
                "Entra tenant mismatch for oid=%s: expected tid=%s, got tid=%s",
                oid,
                user.entra_tenant_id,
                tid,
            )
            raise EntraAccessDeniedError(
                "Account tenant mismatch detected.",
                user_message="Your Microsoft account tenant does not match the linked FinanceDesk record.",
            )
    else:
        # Attempt controlled one-time email match if enabled
        if getattr(settings, "MICROSOFT_LINK_EXISTING_BY_EMAIL", True) and email:
            matches = list(User.objects.select_for_update().filter(email__iexact=email))
            if len(matches) > 1:
                logger.warning(
                    "Refusing Entra email link for oid=%s: multiple local users share email.",
                    oid,
                )
                raise EntraAccountConflictError(
                    "Multiple local accounts match your email address. Please contact an administrator to link your account."
                )
            if len(matches) == 1:
                candidate = matches[0]
                if candidate.entra_object_id and candidate.entra_object_id != oid:
                    logger.warning(
                        "Refusing Entra email link for oid=%s: local user %s is already linked to another oid.",
                        oid,
                        candidate.username,
                    )
                    raise EntraAccountConflictError(
                        "Your local FinanceDesk account is already linked to a different Microsoft identity."
                    )
                user = candidate
                linked_existing = True

    if user is not None and not user.is_active:
        logger.warning(
            "Rejected Entra login for disabled user username=%s oid=%s",
            user.username,
            oid,
        )
        raise EntraAccessDeniedError(
            "Your FinanceDesk account is inactive. Please contact an administrator."
        )

    resolved_role, resolved_department = resolve_role_and_department(
        claims,
        existing_user=user,
        access_token=access_token,
    )

    if user is None:
        if not getattr(settings, "MICROSOFT_AUTO_PROVISION_USERS", False):
            logger.info(
                "Rejected unlinked Entra login for oid=%s (MICROSOFT_AUTO_PROVISION_USERS=False).",
                oid,
            )
            raise EntraAccessDeniedError(
                "Your Microsoft account is not enrolled in FinanceDesk. Please ask an administrator to enroll you."
            )
        username = _generate_unique_username(email, oid)
        user = User(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=resolved_role or User.ROLE_EMPLOYEE,
            department=resolved_department or "",
            is_active=True,
            must_change_password=False,
            entra_object_id=oid,
            entra_tenant_id=tid,
            entra_last_sync_at=timezone.now(),
        )
        user.set_unusable_password()
        user.full_clean()
        user.save()
        created = True
        role_updated = True
        logger.info(
            "Auto-provisioned Entra user username=%s oid=%s tid=%s role=%s",
            user.username,
            oid,
            tid,
            user.role,
        )
        return EntraSyncResult(
            user=user,
            created=created,
            linked_existing=linked_existing,
            role_updated=role_updated,
        )

    # Update linked or existing user fields safely
    role_updated = False
    if resolved_role and user.role != resolved_role:
        user.role = resolved_role
        role_updated = True
    if resolved_department and user.department != resolved_department:
        user.department = resolved_department
    if email and not user.email:
        user.email = email
    if first_name and not user.first_name:
        user.first_name = first_name
    if last_name and not user.last_name:
        user.last_name = last_name

    user.entra_object_id = oid
    user.entra_tenant_id = tid
    user.entra_last_sync_at = timezone.now()
    # Users authenticated via corporate Entra ID SSO do not need a temporary local password reset
    if user.must_change_password:
        user.must_change_password = False

    user.full_clean()
    user.save()
    logger.info(
        "Authenticated Entra user username=%s oid=%s tid=%s role=%s linked_existing=%s role_updated=%s",
        user.username,
        oid,
        tid,
        user.role,
        linked_existing,
        role_updated,
    )
    return EntraSyncResult(
        user=user,
        created=created,
        linked_existing=linked_existing,
        role_updated=role_updated,
    )
