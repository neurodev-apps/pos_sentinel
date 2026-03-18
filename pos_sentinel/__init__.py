# -*- coding: utf-8 -*-

from . import models
from . import wizard


def uninstall_hook(env):
    """Clean up system parameters on uninstall.

    Audit event records are NOT deleted — they remain as permanent
    compliance records even after the module is removed.
    """
    from .models.pos_audit_engine import invalidate_salt_cache

    ICP = env['ir.config_parameter'].sudo()
    for key in [
        'pos_sentinel.hash_salt',
        'pos_sentinel.last_integrity_check',
        'pos_sentinel.last_tampered_count',
        'pos_sentinel.retention_days',
        'pos_sentinel.threshold_low',
        'pos_sentinel.threshold_medium',
        'pos_sentinel.threshold_high',
        'pos_sentinel.threshold_critical',
    ]:
        ICP.set_param(key, '')

    invalidate_salt_cache()
