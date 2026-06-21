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
    "#7C3AED", "#21B799", "#E6007E", "#5B5EA6",
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
        this.notification = useService("notification");
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
        this._liveInterval = null;

        onMounted(async () => {
            await loadBundle("web.chartjs_lib");
            await this.loadData();
            this._liveInterval = setInterval(() => this._refreshLiveFeed(), 60000);
        });

        onWillUnmount(() => {
            if (this._chartTimeout) { clearTimeout(this._chartTimeout); this._chartTimeout = null; }
            if (this._liveInterval) { clearInterval(this._liveInterval); this._liveInterval = null; }
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
                { date_from: dateFrom.toISOString(), date_to: now.toISOString() }
            );
            this.state.data = data;
            this.state.loading = false;
            this._chartTimeout = setTimeout(() => {
                this._chartTimeout = null;
                this.renderCharts();
            }, 0);
        } catch (e) {
            console.error("[POS Sentinel] Dashboard load error:", e);
            this.notification.add(_t("Failed to load dashboard data."), { type: "danger" });
            this.state.loading = false;
        }
    }

    async _refreshLiveFeed() {
        if (!this.state.data) return;
        try {
            const days = parseInt(this.state.period);
            const now = new Date();
            const dateFrom = new Date(now - days * 24 * 60 * 60 * 1000);
            const data = await this.orm.call(
                "pos.sentinel.dashboard",
                "get_dashboard_data",
                [],
                { date_from: dateFrom.toISOString(), date_to: now.toISOString() }
            );
            if (this.state.data) {
                this.state.data = { ...this.state.data, recent_critical: data.recent_critical, summary: data.summary };
            }
        } catch (e) {
            // Silent — do not disturb user on background refresh
        }
    }

    async onPeriodChange(ev) {
        this.state.period = ev.target.value;
        this.destroyCharts();
        await this.loadData();
    }

    onViewEvents() { this.action.doAction("pos_sentinel.action_pos_audit_event"); }
    onViewRules() { this.action.doAction("pos_sentinel.action_pos_scoring_rule"); }

    exportCsv() {
        const events = this.recentCritical;
        if (!events.length) {
            this.notification.add(_t("No events to export."), { type: "warning" });
            return;
        }
        const headers = ["Time", "Type", "User", "Risk Level", "Score"];
        const rows = events.map(e => [
            this.formatDate(e.create_date),
            this.getEventTypeLabel(e.event_type),
            e.user_name,
            (e.risk_level || "").toUpperCase(),
            this.formatScore(e.risk_score),
        ]);
        // PS-CR-03: neutralise CSV/formula injection — cells starting with
        // =, +, -, @, tab or CR get a leading quote so spreadsheets treat them
        // as text, not formulas.
        const sanitize = (v) => {
            let s = String(v);
            if (/^[=+\-@\t\r]/.test(s)) {
                s = "'" + s;
            }
            return `"${s.replace(/"/g, '""')}"`;
        };
        const csv = [headers, ...rows].map(r => r.map(sanitize).join(",")).join("\n");
        const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `pos_sentinel_${new Date().toISOString().split("T")[0]}.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    // ── Charts ────────────────────────────────────────────────────

    destroyCharts() {
        for (const key in this.charts) {
            if (this.charts[key]) { this.charts[key].destroy(); delete this.charts[key]; }
        }
    }

    renderCharts() {
        if (!this.state.data || !window.Chart) return;
        this.destroyCharts();
        for (const method of ["renderByDayChart", "renderByTypeChart", "renderByRiskChart", "renderByUserChart"]) {
            try { this[method](); } catch (e) { console.error(`[POS Sentinel] ${method} failed:`, e); }
        }
    }

    renderByDayChart() {
        const el = this.chartRefs.byDay?.el;
        if (!el) return;
        const data = this.state.data.by_day || [];
        this.charts.byDay = new Chart(el, {
            type: "line",
            data: {
                labels: data.map(d => d.day),
                datasets: [
                    { label: _t("Total Events"), data: data.map(d => d.total), borderColor: "#7C3AED", backgroundColor: "rgba(124,58,237,0.08)", fill: true, tension: 0.3, pointRadius: 3 },
                    { label: _t("High/Critical"), data: data.map(d => d.risky), borderColor: "#DC3545", backgroundColor: "rgba(220,53,69,0.08)", fill: true, tension: 0.3, pointRadius: 3 },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { position: "top", labels: { boxWidth: 10, font: { size: 11 } } } },
                scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
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
                labels: data.map(d => getEventTypeLabel(d.event_type)),
                datasets: [{ data: data.map(d => d.count), backgroundColor: CHART_COLORS.slice(0, data.length), borderWidth: 2 }],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { position: "right", labels: { boxWidth: 10, font: { size: 11 } } } },
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
                labels: data.map(d => (d.risk_level || "none").toUpperCase()),
                datasets: [{ label: _t("Events"), data: data.map(d => d.count), backgroundColor: data.map(d => RISK_COLORS[d.risk_level] || RISK_COLORS.none), borderRadius: 4 }],
            },
            options: {
                indexAxis: "y", responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { x: { beginAtZero: true, ticks: { precision: 0 } } },
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
                labels: data.map(d => d.user_name || d.user_login),
                datasets: [
                    { label: _t("Total Risk Score"), data: data.map(d => parseFloat(d.total_score) || 0), backgroundColor: "#7C3AED", borderRadius: 4 },
                    { label: _t("Events"), data: data.map(d => d.event_count), backgroundColor: "#17A2B8", borderRadius: 4 },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { position: "top", labels: { boxWidth: 10, font: { size: 11 } } } },
                scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
            },
        });
    }

    // ── Heat map helpers ──────────────────────────────────────────

    get heatmapUsers() { return this.state.data?.heatmap_users || []; }
    get heatmapHours() { return [6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,0,1,2,3,4,5]; }

    getHeatmapCellStyle(user, hour) {
        const rows = this.state.data?.heatmap || [];
        const entry = rows.find(d => d.user_name === user && d.hour === hour);
        const risk = entry ? entry.risk_sum : 0;
        const maxRisk = this.state.data?.heatmap_max || 1;
        const ratio = Math.min(risk / maxRisk, 1);
        const color = ratio === 0 ? "#f0f4f8" : this._riskColor(ratio);
        return `background-color:${color};`;
    }

    getHeatmapCellTitle(user, hour) {
        const rows = this.state.data?.heatmap || [];
        const entry = rows.find(d => d.user_name === user && d.hour === hour);
        if (!entry || entry.risk_sum === 0) return `${user} · ${hour}:00 — no events`;
        return `${user} · ${hour}:00 — ${entry.event_count} events, score ${entry.risk_sum.toFixed(0)}`;
    }

    _riskColor(ratio) {
        const stops = [
            [0.00, [240, 244, 248]],
            [0.33, [255, 193, 7]],
            [0.66, [253, 126, 20]],
            [1.00, [220, 53, 69]],
        ];
        let lo = stops[0], hi = stops[stops.length - 1];
        for (let i = 0; i < stops.length - 1; i++) {
            if (ratio >= stops[i][0] && ratio <= stops[i + 1][0]) { lo = stops[i]; hi = stops[i + 1]; break; }
        }
        const t = (hi[0] === lo[0]) ? 0 : (ratio - lo[0]) / (hi[0] - lo[0]);
        const r = Math.round(lo[1][0] + (hi[1][0] - lo[1][0]) * t);
        const g = Math.round(lo[1][1] + (hi[1][1] - lo[1][1]) * t);
        const b = Math.round(lo[1][2] + (hi[1][2] - lo[1][2]) * t);
        return `rgb(${r},${g},${b})`;
    }

    // ── Delta comparison helpers ──────────────────────────────────

    get prevSummary() { return this.state.data?.prev_summary || {}; }

    getDelta(currentKey) {
        const c = parseFloat(this.summary[currentKey]) || 0;
        const p = parseFloat(this.prevSummary[currentKey]) || 0;
        if (p === 0) return null;
        const pct = Math.round((c - p) / p * 100);
        return { pct: Math.abs(pct), up: pct > 0, zero: pct === 0 };
    }

    getCriticalHighDelta() {
        const c = (parseFloat(this.summary.critical) || 0) + (parseFloat(this.summary.high) || 0);
        const p = (parseFloat(this.prevSummary.critical) || 0) + (parseFloat(this.prevSummary.high) || 0);
        if (p === 0) return null;
        const pct = Math.round((c - p) / p * 100);
        return { pct: Math.abs(pct), up: pct > 0, zero: pct === 0 };
    }

    // ── Getters ───────────────────────────────────────────────────

    get summary() { return this.state.data?.summary || {}; }
    get integrity() { return this.state.data?.integrity || {}; }
    get recentCritical() { return this.state.data?.recent_critical || []; }
    get topProducts() { return this.state.data?.top_products || []; }

    getEventTypeLabel(type) { return getEventTypeLabel(type); }

    getRiskBadgeClass(level) {
        return { critical: "bg-danger", high: "bg-warning text-dark", medium: "bg-info text-dark", low: "bg-secondary", none: "bg-light text-dark" }[level] || "bg-light text-dark";
    }

    formatScore(score) { return parseFloat(score || 0).toFixed(1); }

    formatDate(dateStr) {
        if (!dateStr) return "";
        return new Date(dateStr).toLocaleString();
    }
}

registry.category("actions").add("pos_sentinel_dashboard", PosSentinelDashboard);
