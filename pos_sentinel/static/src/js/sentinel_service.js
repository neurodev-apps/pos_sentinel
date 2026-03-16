/** @odoo-module */

/**
 * POS Sentinel — Event Queue Service
 *
 * Manages a queue of POS audit events and sends them to the backend
 * in batches via orm.call(). Uses fire-and-forget pattern: errors are
 * caught and logged, never shown to the cashier.
 *
 * The service exposes itself as window.__posSentinel for access from
 * POS model patches (which don't have access to env.services).
 */

import { registry } from "@web/core/registry";

const BATCH_SIZE = 20;
const FLUSH_INTERVAL_MS = 5000;
const MAX_QUEUE_SIZE = 500;
const MAX_FLUSH_RETRIES = 3;

export const sentinelService = {
    dependencies: ["orm"],

    start(env, { orm }) {
        const queue = [];
        let flushTimer = null;
        let isFlushing = false;

        function scheduleFlush() {
            if (flushTimer) {
                return;
            }
            flushTimer = setTimeout(() => {
                flushTimer = null;
                flush();
            }, FLUSH_INTERVAL_MS);
        }

        async function flush() {
            if (isFlushing || queue.length === 0) {
                return;
            }
            isFlushing = true;
            const batch = queue.splice(0, BATCH_SIZE);
            try {
                await orm.call("pos.audit.event", "log_events_batch", [batch]);
            } catch (e) {
                console.warn("[POS Sentinel] Failed to flush events:", e);
                // Re-queue only if we haven't exceeded max size
                if (queue.length + batch.length <= MAX_QUEUE_SIZE) {
                    queue.unshift(...batch);
                }
            } finally {
                isFlushing = false;
                if (queue.length > 0) {
                    scheduleFlush();
                }
            }
        }

        function logEvent(eventType, data = {}) {
            // Drop events if queue is full to prevent memory issues
            if (queue.length >= MAX_QUEUE_SIZE) {
                return;
            }
            const event = {
                event_type: eventType,
                pos_session_id: data.pos_session_id || false,
                pos_order_id: data.pos_order_id || false,
                pos_config_id: data.pos_config_id || false,
                product_id: data.product_id || false,
                employee_id: data.employee_id || false,
                amount: data.amount || 0,
                details: data.details || {},
            };
            queue.push(event);

            if (queue.length >= BATCH_SIZE) {
                flush();
            } else {
                scheduleFlush();
            }
        }

        async function flushNow() {
            if (flushTimer) {
                clearTimeout(flushTimer);
                flushTimer = null;
            }
            let retries = 0;
            while (queue.length > 0 && retries < MAX_FLUSH_RETRIES) {
                const prevLen = queue.length;
                await flush();
                // If queue didn't shrink, we're stuck — break to avoid infinite loop
                if (queue.length >= prevLen) {
                    retries++;
                } else {
                    retries = 0;
                }
            }
        }

        const service = { logEvent, flushNow, getQueueSize: () => queue.length };

        // Expose globally for POS model patches (which lack env.services)
        window.__posSentinel = service;

        return service;
    },
};

registry.category("services").add("pos_sentinel", sentinelService);
