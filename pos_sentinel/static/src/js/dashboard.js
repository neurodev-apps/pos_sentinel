/** @odoo-module */

import { Component, onMounted, onWillUnmount, useState, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";

const EVENT_TYPE_KEYS = {
    void_line: "Line Void",
    price_override: "Price Override",
    discount: "Discount",
    refund: "Refund",
    cash_in: "Cash In",
    cash_out: "Cash Out",
    order_delete: "Order Deleted",
    line_qty_change: "Qty Changed",
    payment_change: "Payment Modified",
    session_open: "Session Open",
    session_close: "Session Close",
    order_complete: "Order Complete",
    manual_price: "Manual Price",
    negative_qty: "Negative Qty",
    post_payment_edit: "Post-Payment Edit",
    sequence_gap: "Sequence Gap",
    other: "Other",
};

function getEventTypeLabel(type) {
    const key = EVENT_TYPE_KEYS[type];
    return key ? _t(key) : type;
}

const RISK_COLORS = {
    critical: "#DC3545",
    high: "#FD7E14",
    medium: "#FFC107",
    low: "#17A2B8",
    none: "#6C757D",
};

const CHART_COLORS = [
    "#875A7B", "#21B799", "#E6007E", "#5B5EA6",
    "#F39C12", "#3498DB", "#E74C3C", "#2ECC71",
    "#9B59B6", "#1ABC9C", "#E67E22", "#34495E",
    "#16A085", "#D35400", "#8E44AD", "#27AE60",
    "#C0392B",
];

export class PosSentinelDashboard extends Component {
    static template = "pos_sentinel.Dashboard";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            data: null,
            loading: true,
            period: "30",
        });
        this.chartRefs = {
            byDay: useRef("chartByDay"),
            byType: useRef("chartByType"),
            byRisk: useRef("chartByRisk"),
            byUser: useRef("chartByUser"),
        };
        this.charts = {};
        this._chartTimeout = null;

        onMounted(async () => {
            await loadBundle("web.chartjs_lib");
            await this.loadData();
        });

        onWillUnmount(() => {
            if (this._chartTimeout) {
                clearTimeout(this._chartTimeout);
                this._chartTimeout = null;
            }
            this.destroyCharts();
        });
    }

    async loadData() {
        this.state.loading = true;
        try {
            const days = parseInt(this.state.period);
            const now = new Date();
            const dateFrom = new Date(now - days * 24 * 60 * 60 * 1000);
            const data = await this.orm.call(
                "pos.sentinel.dashboard",
                "get_dashboard_data",
                [],
                {
                    date_from: dateFrom.toISOString(),
                    date_to: now.toISOString(),
                }
            );
            this.state.data = data;
            this.state.loading = false;
            // Use setTimeout to ensure DOM is updated before rendering charts
            this._chartTimeout = setTimeout(() => {
                this._chartTimeout = null;
                this.renderCharts();
            }, 0);
        } catch (e) {
            console.error("[POS Sentinel] Dashboard load error:", e);
            this.state.loading = false;
        }
    }

    async onPeriodChange(ev) {
        this.state.period = ev.target.value;
        this.destroyCharts();
        await this.loadData();
    }

    onViewEvents() {
        this.action.doAction("pos_sentinel.action_pos_audit_event");
    }

    onViewRules() {
        this.action.doAction("pos_sentinel.action_pos_scoring_rule");
    }

    // ── Chart rendering ──────────────────────────────────────────

    destroyCharts() {
        for (const key in this.charts) {
            if (this.charts[key]) {
                this.charts[key].destroy();
                delete this.charts[key];
            }
        }
    }

    renderCharts() {
        if (!this.state.data || !window.Chart) return;
        this.destroyCharts();
        this.renderByDayChart();
        this.renderByTypeChart();
        this.renderByRiskChart();
        this.renderByUserChart();
    }

    renderByDayChart() {
        const el = this.chartRefs.byDay?.el;
        if (!el) return;
        const data = this.state.data.by_day || [];

        this.charts.byDay = new Chart(el, {
            type: "line",
            data: {
                labels: data.map((d) => d.day),
                datasets: [
                    {
                        label: _t("Total Events"),
                        data: data.map((d) => d.total),
                        borderColor: "#875A7B",
                        backgroundColor: "rgba(135, 90, 123, 0.1)",
                        fill: true,
                        tension: 0.3,
                    },
                    {
                        label: _t("High/Critical"),
                        data: data.map((d) => d.risky),
                        borderColor: "#DC3545",
                        backgroundColor: "rgba(220, 53, 69, 0.1)",
                        fill: true,
                        tension: 0.3,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: "top" } },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0 } },
                },
            },
        });
    }

    renderByTypeChart() {
        const el = this.chartRefs.byType?.el;
        if (!el) return;
        const data = this.state.data.by_type || [];

        this.charts.byType = new Chart(el, {
            type: "doughnut",
            data: {
                labels: data.map((d) => getEventTypeLabel(d.event_type)),
                datasets: [
                    {
                        data: data.map((d) => d.count),
                        backgroundColor: CHART_COLORS.slice(0, data.length),
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: "right", labels: { boxWidth: 12 } },
                },
            },
        });
    }

    renderByRiskChart() {
        const el = this.chartRefs.byRisk?.el;
        if (!el) return;
        const data = this.state.data.by_risk || [];

        this.charts.byRisk = new Chart(el, {
            type: "bar",
            data: {
                labels: data.map((d) => (d.risk_level || "none").toUpperCase()),
                datasets: [
                    {
                        label: _t("Events"),
                        data: data.map((d) => d.count),
                        backgroundColor: data.map(
                            (d) => RISK_COLORS[d.risk_level] || RISK_COLORS.none
                        ),
                    },
                ],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { beginAtZero: true, ticks: { precision: 0 } },
                },
            },
        });
    }

    renderByUserChart() {
        const el = this.chartRefs.byUser?.el;
        if (!el) return;
        const data = this.state.data.by_user || [];

        this.charts.byUser = new Chart(el, {
            type: "bar",
            data: {
                labels: data.map((d) => d.user_name || d.user_login),
                datasets: [
                    {
                        label: _t("Total Risk Score"),
                        data: data.map((d) => parseFloat(d.total_score) || 0),
                        backgroundColor: "#875A7B",
                    },
                    {
                        label: _t("Events"),
                        data: data.map((d) => d.event_count),
                        backgroundColor: "#17A2B8",
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: "top" } },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0 } },
                },
            },
        });
    }

    // ── Helpers ───────────────────────────────────────────────────

    get summary() {
        return this.state.data?.summary || {};
    }

    get integrity() {
        return this.state.data?.integrity || {};
    }

    get recentCritical() {
        return this.state.data?.recent_critical || [];
    }

    get topProducts() {
        return this.state.data?.top_products || [];
    }

    getEventTypeLabel(type) {
        return getEventTypeLabel(type);
    }

    getRiskBadgeClass(level) {
        const map = {
            critical: "bg-danger",
            high: "bg-warning text-dark",
            medium: "bg-info",
            low: "bg-secondary",
            none: "bg-light text-dark",
        };
        return map[level] || "bg-light text-dark";
    }

    formatScore(score) {
        return parseFloat(score || 0).toFixed(1);
    }

    formatDate(dateStr) {
        if (!dateStr) return "";
        const d = new Date(dateStr);
        return d.toLocaleString();
    }
}

registry.category("actions").add("pos_sentinel_dashboard", PosSentinelDashboard);
