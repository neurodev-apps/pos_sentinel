# -*- coding: utf-8 -*-

import hashlib
import hmac
import json
import logging
import secrets
import threading
from datetime import datetime as dt

_logger = logging.getLogger(__name__)

# Salt cache keyed by DB name — multi-DB safe
_salt_cache = {}
_salt_lock = threading.Lock()

# Hash algorithm versions stored on each event (PS-CR-01/02).
#   None / 1 -> legacy salted SHA-256 (events created before the upgrade)
#   2        -> HMAC-SHA256 over the full payload + previous_hash (chained)
HASH_VERSION_HMAC_CHAIN = 2

# Fixed key for the PostgreSQL advisory lock that serialises hash-chain writes
# so concurrent transactions never fork the chain (PS-CR-02).
_CHAIN_LOCK_KEY = 5417823094


def _build_event_payload_v2(fields):
    """Canonical JSON payload hashed by the v2 algorithm (PS-CR-01/02).

    Centralised so creation, ``_recompute_hash`` and the integrity cron all
    produce byte-for-byte identical payloads. Covers far more than v1: adds
    company_id, amount, risk_level and the chain link previous_hash, so that
    altering any of those — or deleting an intermediate record — is detected.
    ``company_id`` is normalised to int-or-False (never None) for SQL/ORM parity.
    """
    company_id = fields.get('company_id')
    return json.dumps(
        {
            'user_id': fields.get('user_id'),
            'event_type': fields.get('event_type') or '',
            'pos_session_id': fields.get('pos_session_id') or 0,
            'pos_order_id': fields.get('pos_order_id') or 0,
            'company_id': company_id or False,
            # float() so a Monetary read back as Decimal via raw SQL serialises
            # identically to the float used at creation time.
            'amount': float(fields.get('amount') or 0.0),
            'risk_level': fields.get('risk_level') or '',
            'create_date': fields.get('create_date') or '',
            'details': fields.get('details') or '',
            'previous_hash': fields.get('previous_hash') or '',
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def compute_event_hash_v2(env, fields):
    """HMAC-SHA256 of the v2 payload, keyed with the sentinel secret (PS-CR-01)."""
    secret = get_sentinel_salt(env)
    payload = _build_event_payload_v2(fields)
    return hmac.new(
        secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256
    ).hexdigest()


def invalidate_salt_cache():
    """Clear the salt cache for all databases."""
    with _salt_lock:
        _salt_cache.clear()


def get_sentinel_salt(env):
    """Retrieve the hash salt from ir.config_parameter.

    If ``pos_sentinel.hash_salt`` does not exist, a cryptographically-secure
    random 64-char hex salt is generated and persisted.

    The result is cached per database name so multi-DB Odoo processes each
    use the correct salt.  Access is serialised with ``_salt_lock``.
    """
    dbname = env.cr.dbname
    with _salt_lock:
        if dbname in _salt_cache:
            return _salt_cache[dbname]

        ICP = env['ir.config_parameter'].sudo()
        salt = ICP.get_param('pos_sentinel.hash_salt')
        if not salt:
            salt = secrets.token_hex(32)
            ICP.set_param('pos_sentinel.hash_salt', salt)
            _logger.info("POS Sentinel: generated new hash salt for DB %s", dbname)

        _salt_cache[dbname] = salt
        return salt


def compute_event_hash(env, user_id, event_type, pos_session_id, pos_order_id,
                       create_date, details):
    """Generate SHA-256 hash for a POS audit event.

    Uses pipe-delimited format with strftime to avoid isoformat()
    microsecond inconsistencies.

    Args:
        env: Odoo environment.
        user_id: UID of the cashier/user.
        event_type: Event type code (e.g. 'void_line').
        pos_session_id: Database ID of the POS session.
        pos_order_id: Database ID of the POS order (0 if N/A).
        create_date: datetime of event creation.
        details: JSON string with event details.

    Returns:
        64-char hex SHA-256 digest.
    """
    salt = get_sentinel_salt(env)
    if isinstance(create_date, dt):
        date_str = create_date.strftime('%Y-%m-%d %H:%M:%S')
    elif create_date is None:
        date_str = ''
    else:
        date_str = str(create_date)[:19]

    hash_input = (
        f"{user_id}|{event_type}|{pos_session_id}|{pos_order_id}"
        f"|{date_str}|{details}|{salt}"
    )
    return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()
