# -*- coding: utf-8 -*-

import hashlib
import logging
import secrets
import threading
from datetime import datetime as dt

_logger = logging.getLogger(__name__)

# Salt cache keyed by DB name — multi-DB safe
_salt_cache = {}
_salt_lock = threading.Lock()


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
