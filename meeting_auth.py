"""Optional VM identity that preserves the Cloud Run service-account identity."""
import os

import google.auth
from google.auth import impersonated_credentials
from google.auth.compute_engine import Credentials
from google.auth.transport.requests import Request

CLOUD_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'


def default_credentials(scopes=None):
    account = os.getenv('MEETING_VM_SERVICE_ACCOUNT')
    if not account:
        return google.auth.default(scopes=scopes)
    source = Credentials(scopes=[CLOUD_SCOPE])
    target = impersonated_credentials.Credentials(
        source_credentials=source, target_principal=account,
        target_scopes=scopes or [CLOUD_SCOPE], lifetime=3600)
    return target, os.environ['GOOGLE_CLOUD_PROJECT']


def vm_id_token(audience):
    target, _ = default_credentials([CLOUD_SCOPE])
    if not isinstance(target, impersonated_credentials.Credentials):
        raise ValueError('VM impersonation is not configured')
    credentials = impersonated_credentials.IDTokenCredentials(
        target, target_audience=audience, include_email=True)
    credentials.refresh(Request())
    return credentials.token
