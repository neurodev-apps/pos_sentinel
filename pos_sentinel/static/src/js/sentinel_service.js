/** @odoo-module */

/**
 * POS Sentinel — Event Queue Service
 *
 * Manages a queue of POS audit events and sends them to the backend
 * in batches via orm.call(). Uses fire-and-forget pattern: errors are
 * caught and logged, never shown to the cashier.
 *
 * The service is exposed via the module-level getSentinel() helper for the
 * POS model patches (which lack env.services) — NOT on window, so the audited
 * cashier cannot forge events from the browser console (PS-CR-09).
 */

import { registry } from "@web/core/registry";

const BATCH_SIZE = 20;
const FLUSH_INTERVAL_MS = 5000;
const MAX_QUEUE_SIZE = 500;
const MAX_FLUSH_RETRIES = 3;

// Module-level reference to the running service instance. The POS model patches
// (which lack env.services) read it via getSentinel() instead of a global window
// property, so the audited cashier cannot forge events from the console (PS-CR-09).
let _sentinelInstance = null;

export function getSentinel() {
    return _sentinelInstance;
}

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

        // PS-CR-09: expose via module reference, NOT on window.
        _sentinelInstance = service;

        window.addEventListener("beforeunload", () => {
            if (flushTimer) {
                clearTimeout(flushTimer);
                flushTimer = null;
            }
            // PS-MD-05: best-effort flush of queued events before unload.
            try {
                flush();
            } catch (e) {
                // fire-and-forget — never block unload
            }
            _sentinelInstance = null;
        });

        return service;
    },
};

registry.category("services").add("pos_sentinel", sentinelService);
