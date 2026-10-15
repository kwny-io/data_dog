// 本地预览用 mock 后端（仅用于给人看界面，不是产品代码）
//
// 目的：在真实后端就绪前，用一批「接近真实」的数据把前端所有面板喂满，
//       以便评估 UI 的视觉与交互。数据刻意做成多个候选 / 多个告警状态 /
//       多份报告，而不是测试里那种单条最小 fixture。
//
// 用法：
//   node devmock/mock-backend.mjs          # 监听 :8000
//   然后另开一个终端：NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev --prefix web
//
// 开关：
//   EMPTY=1  所有列表数据集为空（看空状态）
//   ASYNC=1  调查走异步 job（202 + 轮询 + 取结果），E2E 用它覆盖客户端轮询链路

import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? 8000);
const TENANT = "tenant-demo";

// EMPTY=1 → serve zero items everywhere, to review the empty-state UI.
// Metrics stay (a live service still reports health), so the dashboard shows
// its "no data yet" panels rather than failing.
const EMPTY = process.env.EMPTY === "1";

// ASYNC=1 → POST /v1/investigations 回 202 {job_id}，并实现 job 的轮询/取结果/取消。
// 默认（不设）保持同步长任务，人工预览时行为一字不变。
// E2E 用 ASYNC=1：只有让真浏览器走一遍「提交 → 轮询 → 取结果」，
// 才证明得了客户端的异步轮询链路（jsdom 里那套是 mock 出来的轮询）。
const ASYNC = process.env.ASYNC === "1";

/** 异步 job 台账（内存）。状态机：running →（第 2 次轮询）succeeded。 */
const JOBS = new Map();
let jobSeq = 0;

/** job 对外形状：只在**终态**才带 report_id，与真实后端的 _job_public 一致。 */
function jobPublic(job) {
  return {
    job_id: job.job_id,
    candidate_id: job.candidate_id,
    status: job.status,
    report_id: job.report_id,
    error_code: job.error_code,
  };
}

/** 读请求体。devmock 其余路由都不看正文，只有调查 POST 需要 candidate_id。 */
function readJson(req) {
  return new Promise((resolve) => {
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("error", () => resolve({}));
    req.on("end", () => {
      try { resolve(raw ? JSON.parse(raw) : {}); } catch { resolve({}); }
    });
  });
}

// ---------------------------------------------------------------- 数据

const CANDIDATES = [
  {
    candidate_id: "cand_ab12cd34ef56",
    device_sn: "GO2-EDU-0417",
    component_ref: "joint_motor.front_left_hip",
    signal: "temperature_rising_under_normalized_load",
    severity_hint: "critical",
    window_start: "2026-08-13T10:05:00Z",
    window_end: "2026-08-13T10:06:00Z",
    sample_count: 42,
    rule_matches: ["MOTOR_TEMP_PROTECT"],
    anomaly_score: 0.94,
    evidence: [
      { key: "temperature_c", from_value: 58, to_value: 96, baseline: 61, unit: "°C", note: "左前髋关节电机" },
      { key: "current_a", from_value: 4.1, to_value: 12.8, baseline: 4.5, unit: "A", note: "同窗口电流抬升" },
      { key: "torque_nm", from_value: 6.2, to_value: 14.7, baseline: 6.5, unit: "N·m", note: null },
    ],
    evidence_refs: [
      "metricwin://cand_ab12cd34ef56",
      "rawlog://evt_ab12cd34ef56_1003",
    ],
    detector_provenance: "op.temperature_protect:1",
  },
  {
    candidate_id: "cand_77aa11bb22cc",
    device_sn: "GO2-EDU-0417",
    component_ref: "joint_motor.rear_right_knee",
    signal: "current_spike",
    severity_hint: "warning",
    window_start: "2026-08-13T10:11:00Z",
    window_end: "2026-08-13T10:12:00Z",
    sample_count: 17,
    rule_matches: ["MOTOR_CURRENT_SPIKE"],
    anomaly_score: 0.71,
    evidence: [
      { key: "current_a", from_value: 3.9, to_value: 9.6, baseline: 4.2, unit: "A", note: "后右膝关节电机" },
      { key: "temperature_c", from_value: 44, to_value: 63, baseline: 46, unit: "°C", note: null },
    ],
    evidence_refs: ["metricwin://cand_77aa11bb22cc"],
    detector_provenance: "op.current_spike:2",
  },
  {
    candidate_id: "cand_9988ff0011ee",
    device_sn: "GO2-EDU-0932",
    component_ref: "battery.bms",
    signal: "bus_voltage_sag",
    severity_hint: "warning",
    window_start: "2026-08-13T09:58:00Z",
    window_end: "2026-08-13T09:59:00Z",
    sample_count: 88,
    rule_matches: ["BMS_VOLTAGE_SAG"],
    anomaly_score: 0.66,
    evidence: [
      { key: "bus_voltage_v", from_value: 49.2, to_value: 41.4, baseline: 49.0, unit: "V", note: "负载突加时母线跌落" },
      { key: "soc_pct", from_value: 78, to_value: 74, baseline: 78, unit: "%", note: null },
    ],
    evidence_refs: ["rawlog://evt_9988ff0011ee_1015"],
    detector_provenance: "op.bms_voltage:1",
  },
  {
    candidate_id: "cand_5544dd6677aa",
    device_sn: "GO2-EDU-0932",
    component_ref: "joint_motor.front_right_hip",
    signal: "position_error",
    severity_hint: "info",
    window_start: "2026-08-13T09:40:00Z",
    window_end: "2026-08-13T09:41:00Z",
    sample_count: 9,
    rule_matches: ["CONTROL_TRACKING_ERROR"],
    anomaly_score: 0.33,
    evidence: [
      { key: "position_err_rad", from_value: 0.02, to_value: 0.11, baseline: 0.02, unit: "rad", note: "轻微跟随误差" },
    ],
    evidence_refs: [],
    detector_provenance: "op.tracking_error:1",
  },
];

const ALERTS = [
  {
    event_id: "alert_9f8e7d6c",
    idempotency_key: "idem_temp_fllhip_1006",
    device_ref: "GO2-EDU-0417",
    component_ref: "joint_motor.front_left_hip",
    event_type: "motor_over_temperature",
    state: "confirmed",
    severity: "critical",
    confidence: 0.94,
    detected_at: "2026-08-13T10:06:10Z",
    event_revision: 2,
    summary: "左前髋关节电机温度越过厂商 90°C 保护线，且同窗口电流同步抬升。",
    recommended_actions: ["立即停机检查散热", "检查轴承是否卡滞"],
    evidence_receipt: "evidence-receipt://rcpt_temp_fllhip",
    investigation_id: "inv_cand_ab12cd34ef56",
    policy_version: "policy-1",
    history: [
      { from: null, to: "detected", reason: "", at: 1_786_601_970 },
      { from: "detected", to: "confirmed", reason: "policy:critical_auto_confirm", at: 1_786_601_982 },
    ],
  },
  {
    event_id: "alert_1122aabb",
    idempotency_key: "idem_curr_rrknee_1012",
    device_ref: "GO2-EDU-0417",
    component_ref: "joint_motor.rear_right_knee",
    event_type: "motor_current_spike",
    state: "escalated",
    severity: "warning",
    confidence: 0.71,
    detected_at: "2026-08-13T10:12:20Z",
    event_revision: 3,
    summary: "后右膝关节电机电流短时冲高至 9.6A，已升级待人工处置。",
    recommended_actions: ["检查负载", "观察是否复发"],
    evidence_receipt: "evidence-receipt://rcpt_curr_rrknee",
    investigation_id: "inv_cand_77aa11bb22cc",
    policy_version: "policy-1",
    history: [
      { from: null, to: "detected", reason: "", at: 1_786_602_340 },
      { from: "detected", to: "confirmed", reason: "policy:warning_confirm", at: 1_786_602_360 },
      { from: "confirmed", to: "escalated", reason: "recurrence>=3", at: 1_786_602_900 },
    ],
  },
  {
    event_id: "alert_battery_v",
    idempotency_key: "idem_bms_voltage_0959",
    device_ref: "GO2-EDU-0932",
    component_ref: "battery.bms",
    event_type: "bus_voltage_sag",
    state: "recovering",
    severity: "warning",
    confidence: 0.66,
    detected_at: "2026-08-13T09:59:05Z",
    event_revision: 1,
    summary: "母线电压在负载突加时跌落至 41.4V，当前正在恢复。",
    recommended_actions: ["确认电池健康度"],
    evidence_receipt: "evidence-receipt://rcpt_bms",
    investigation_id: "inv_cand_9988ff0011ee",
    policy_version: "policy-1",
    history: [
      { from: null, to: "detected", reason: "", at: 1_786_601_545 },
      { from: "detected", to: "confirmed", reason: "policy:warning_confirm", at: 1_786_601_560 },
      { from: "confirmed", to: "recovering", reason: "voltage back above floor", at: 1_786_602_100 },
    ],
  },
  {
    event_id: "alert_tracking",
    idempotency_key: "idem_tracking_frhip_0941",
    device_ref: "GO2-EDU-0932",
    component_ref: "joint_motor.front_right_hip",
    event_type: "control_tracking_error",
    state: "resolved",
    severity: "info",
    confidence: 0.33,
    detected_at: "2026-08-13T09:41:30Z",
    event_revision: 1,
    summary: "右前髋关节跟随误差短暂升高，随后自行恢复，判定为已解决。",
    recommended_actions: [],
    evidence_receipt: "evidence-receipt://rcpt_tracking",
    investigation_id: "inv_cand_5544dd6677aa",
    policy_version: "policy-1",
    history: [
      { from: null, to: "detected", reason: "", at: 1_786_600_890 },
      { from: "detected", to: "confirmed", reason: "policy:info_confirm", at: 1_786_600_905 },
      { from: "confirmed", to: "recovering", reason: "err back under floor", at: 1_786_601_200 },
      { from: "recovering", to: "resolved", reason: "sustained normal", at: 1_786_601_500 },
    ],
  },
  {
    event_id: "alert_suppressed",
    idempotency_key: "idem_suppressed_001",
    device_ref: "GO2-EDU-0417",
    component_ref: "joint_motor.front_left_hip",
    event_type: "motor_over_temperature",
    state: "suppressed",
    severity: "warning",
    confidence: 0.58,
    detected_at: "2026-08-13T09:20:00Z",
    event_revision: 1,
    summary: "同一部件在维护窗口内的重复温度告警，已按策略抑制。",
    recommended_actions: [],
    evidence_receipt: "evidence-receipt://rcpt_supp",
    investigation_id: "inv_cand_supp",
    policy_version: "policy-1",
    history: [
      { from: null, to: "detected", reason: "", at: 1_786_599_200 },
      { from: "detected", to: "suppressed", reason: "maintenance_window", at: 1_786_599_220 },
    ],
  },
  {
    event_id: "alert_closed",
    idempotency_key: "idem_closed_001",
    device_ref: "GO2-EDU-0932",
    component_ref: "battery.bms",
    event_type: "bus_voltage_sag",
    state: "closed",
    severity: "info",
    confidence: 0.4,
    detected_at: "2026-08-12T18:00:00Z",
    event_revision: 4,
    summary: "昨日母线电压跌落告警，人工确认电池已更换，关闭。",
    recommended_actions: [],
    evidence_receipt: "evidence-receipt://rcpt_closed",
    investigation_id: "inv_cand_closed",
    policy_version: "policy-1",
    history: [
      { from: null, to: "detected", reason: "", at: 1_786_542_000 },
      { from: "detected", to: "confirmed", reason: "policy:warning_confirm", at: 1_786_542_020 },
      { from: "confirmed", to: "recovering", reason: "battery swapped", at: 1_786_548_000 },
      { from: "recovering", to: "resolved", reason: "nominal", at: 1_786_548_600 },
      { from: "resolved", to: "closed", reason: "operator closed", at: 1_786_549_000 },
    ],
  },
  {
    event_id: "alert_fresh_detected",
    idempotency_key: "idem_fresh_imu_1021",
    device_ref: "GO2-EDU-0932",
    component_ref: "imu.main",
    event_type: "attitude_drift",
    state: "detected",
    severity: "warning",
    confidence: 0.55,
    detected_at: "2026-08-13T10:21:40Z",
    event_revision: 1,
    summary: "IMU 姿态角出现持续漂移，刚触发策略门，等待确认或抑制。",
    recommended_actions: ["复核 IMU 零偏", "如为真则标定"],
    evidence_receipt: "evidence-receipt://rcpt_fresh_imu",
    investigation_id: "inv_cand_fresh_imu",
    policy_version: "policy-1",
    history: [{ from: null, to: "detected", reason: "", at: 1_786_602_100 }],
  },
  {
    event_id: "alert_rejected",
    idempotency_key: "idem_rejected_cam_1008",
    device_ref: "GO2-EDU-0417",
    component_ref: "camera.main",
    event_type: "frame_drop_spike",
    state: "rejected",
    severity: "info",
    confidence: 0.28,
    detected_at: "2026-08-13T10:08:00Z",
    event_revision: 2,
    summary: "相机短时丢帧率升高，经人工判定为环境光照导致，已驳回。",
    recommended_actions: [],
    evidence_receipt: "evidence-receipt://rcpt_rejected",
    investigation_id: "inv_cand_rejected_cam",
    policy_version: "policy-1",
    history: [
      { from: null, to: "detected", reason: "", at: 1_786_601_480 },
      { from: "detected", to: "rejected", reason: "operator: environmental glare", at: 1_786_601_700 },
    ],
  },
];

const REPORTS = ALERTS.filter((a) => a.investigation_id).map((a) => ({
  investigation_id: a.investigation_id,
  disposition: a.severity === "critical" ? "needs_human_review" : "auto_triaged",
  summary: a.summary,
  candidate_refs: [],
  event_type: a.event_type,
  severity: a.severity,
  confidence: a.confidence,
  hypotheses: [
    {
      cause: a.summary,
      status: a.severity === "critical" ? "supported" : "inconclusive",
      evidence_refs: [],
      counter_evidence_refs: [],
    },
  ],
  counter_evidence_refs: [],
  missing_evidence: a.severity === "critical" ? ["缺历史同部件温度基线"] : [],
  risk_window: null,
  recommended_actions: a.recommended_actions,
  model_provenance: { provider: "deepseek", model: "deepseek-chat", call_count: 3, total_tokens: 1234 },
  correlation_group_id: null,
  linked_candidate_refs: [],
}));

const OUTBOX = [
  { id: 1, event_id: "alert_9f8e7d6c", status: "pending", attempts: 0, created_at: "2026-08-13T10:06:12Z" },
  { id: 2, event_id: "alert_1122aabb", status: "pending", attempts: 1, created_at: "2026-08-13T10:12:25Z" },
  { id: 3, event_id: "alert_battery_v", status: "delivered", attempts: 1, created_at: "2026-08-13T09:59:10Z" },
  { id: 4, event_id: "alert_closed", status: "delivered", attempts: 2, created_at: "2026-08-12T18:00:05Z" },
];

const METRICS = EMPTY
  ? `# HELP data_dog_db_rows rows in db
# TYPE data_dog_db_rows gauge
data_dog_db_rows{table="candidates"} 0
data_dog_db_rows{table="telemetry_events"} 0
data_dog_uptime_seconds 98765
data_dog_candidates_created_total 0
`
  : `# HELP data_dog_db_rows rows in db
# TYPE data_dog_db_rows gauge
data_dog_db_rows{table="candidates"} 4
data_dog_db_rows{table="telemetry_events"} 12843
data_dog_uptime_seconds 98765
data_dog_candidates_created_total 4
data_dog_http_requests_total{method="POST",path="/v1/telemetry",status="200"} 1842
data_dog_http_requests_total{method="GET",path="/v1/candidates",status="200"} 271
data_dog_http_requests_total{method="GET",path="/v1/readyz",status="200"} 903
data_dog_http_requests_total{method="GET",path="/v1/candidates",status="500"} 3
data_dog_http_request_duration_seconds_bucket{le="0.05"} 2210
data_dog_http_request_duration_seconds_bucket{le="0.1"} 2760
data_dog_http_request_duration_seconds_bucket{le="0.25"} 2980
data_dog_http_request_duration_seconds_bucket{le="0.5"} 3010
data_dog_http_request_duration_seconds_bucket{le="1"} 3015
data_dog_http_request_duration_seconds_bucket{le="+Inf"} 3019
`;

const INVESTIGATION = {
  ok: true,
  investigation_id: "inv_cand_ab12cd34ef56",
  candidate_id: "cand_ab12cd34ef56",
  report: {
    investigation_id: "inv_cand_ab12cd34ef56",
    disposition: "needs_human_review",
    summary: "左前髋关节电机温度 96°C 越过厂商 90°C 保护线，且电流同步抬升，倾向散热不良/轴承卡滞。",
    candidate_refs: ["cand_ab12cd34ef56"],
    event_type: "motor_over_temperature",
    severity: "critical",
    confidence: 0.72,
    hypotheses: [
      {
        cause: "散热不良导致绕组过热",
        status: "supported",
        evidence_refs: ["metricwin://cand_ab12cd34ef56"],
        counter_evidence_refs: [],
      },
      {
        cause: "轴承卡滞导致负载电流升高",
        status: "inconclusive",
        evidence_refs: ["rawlog://evt_ab12cd34ef56_1003"],
        counter_evidence_refs: [],
      },
    ],
    counter_evidence_refs: [],
    missing_evidence: ["缺历史同部件温度基线", "缺同批次其他设备的对比"],
    risk_window: "2026-08-13T10:05:00Z/2026-08-13T10:06:00Z",
    recommended_actions: ["建议停机检查散热风道", "检查髋关节轴承是否卡滞"],
    model_provenance: { provider: "deepseek", model: "deepseek-chat", call_count: 4, total_tokens: 2871 },
    correlation_group_id: null,
    linked_candidate_refs: [],
  },
  verdict: "⚠️ 检测到 1 条严重异常，建议人工复核",
  verdict_detail: "左前髋关节电机温度 96°C，超过厂商文档写明的 90°C 安全保护线；同窗口电流由 4.1A 升至 12.8A。",
  trace: [
    { type: "tool_call", step: 1, name: "query_candidate_evidence", args: { device_sn: "GO2-EDU-0417", component: "joint_motor.front_left_hip" }, result: { device_sn: "GO2-EDU-0417", component: "joint_motor.front_left_hip", metrics: { temperature_c: 96, current_a: 12.8 } } },
    { type: "tool_call", step: 2, name: "query_vendor_knowledge", args: { query: "motor temperature protection threshold" }, result: { source: "motor_msg_GO-M8010-6.h", line: 42, threshold_c: 90 } },
    { type: "tool_call", step: 3, name: "query_neighbor_events", args: { device_sn: "GO2-EDU-0417", window: "10:05-10:06" }, result: { count: 0 } },
  ],
  usage: { call_count: 4, total_tokens: 2871, cost_usd: 0.0009, cost_estimated: false },
};

// ---------------------------------------------------------------- 路由

// The four list datasets, collapsed to [] when EMPTY=1.
const S_CANDIDATES = EMPTY ? [] : CANDIDATES;
const S_ALERTS = EMPTY ? [] : ALERTS;
const S_REPORTS = EMPTY ? [] : REPORTS;
const S_OUTBOX = EMPTY ? [] : OUTBOX;

function send(res, status, body, contentType = "application/json") {
  const payload = typeof body === "string" ? body : JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": contentType,
    "X-Request-Id": "mock-" + Math.random().toString(16).slice(2, 10),
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
  });
  res.end(payload);
}

const page = (items, limit = 50, offset = 0) => {
  const sliced = items.slice(offset, offset + limit);
  return {
    ok: true,
    count: sliced.length,
    total: items.length,
    limit,
    offset,
    has_more: offset + limit < items.length,
    items: sliced,
  };
};

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const p = url.pathname;
  const q = url.searchParams;
  const limit = Number(q.get("limit") ?? 50);
  const offset = Number(q.get("offset") ?? 0);

  if (req.method === "OPTIONS") return send(res, 204, "");

  if (p === "/healthz") return send(res, 200, { status: "ok", service: "data-dog", version: "0.1.0-mock", uptime_s: 98765 });
  if (p === "/readyz") return send(res, 200, { status: "ready", checks: { database: "ok", migration: "ok", resolver: "ok" } });
  if (p === "/metrics") return send(res, 200, METRICS, "text/plain; version=0.0.4");
  // E2E 探针（**只存在于 devmock，真实后端没有这个端点**）：回显代理**注入**的租户
  // 与密钥，用来从浏览器侧证明两件事——租户由 DD_WEB_TENANT/允许清单 cookie 决定、
  // 密钥由服务端附加。只回布尔与已注入的值，**永不回密钥本身**。
  if (p === "/v1/whoami")
    return send(res, 200, {
      ok: true,
      tenant: req.headers["x-tenant-id"] ?? null,
      api_key_present: Boolean(req.headers["x-api-key"]),
      cookie_forwarded: Boolean(req.headers.cookie),
    });
  if (p === "/v1/summary")
    return send(res, 200, {
      ok: true,
      uptime_s: 98765,
      candidates_total: S_CANDIDATES.length,
      alerts_by_state: S_ALERTS.reduce((m, a) => ((m[a.state] = (m[a.state] ?? 0) + 1), m), {}),
      outbox_pending: S_OUTBOX.filter((o) => o.status === "pending").length,
      resolver_ready: true,
      auth_enabled: true,
    });

  if (p === "/v1/candidates") {
    let items = S_CANDIDATES;
    if (q.get("component_ref")) items = items.filter((c) => c.component_ref === q.get("component_ref"));
    if (q.get("severity")) items = items.filter((c) => c.severity_hint === q.get("severity"));
    return send(res, 200, page(items, limit, offset));
  }
  const candMatch = p.match(/^\/v1\/candidates\/([^/]+)$/);
  if (candMatch) {
    const c = S_CANDIDATES.find((x) => x.candidate_id === candMatch[1]);
    if (!c) return send(res, 404, { ok: false, error: "not_found", detail: "no such candidate" });
    return send(res, 200, { ok: true, item: c });
  }
  // 证据引用解析：与真实后端同形（批解析 + 逐条 ok/error + 截断标注）。
  // 控制台点「解析证据引用」会打这里——devmock 也走真 ref scheme（metricwin/rawlog），
  // 不再用后端 resolver 白名单之外的 evidence://（那样只会得到 invalid_input）。
  const refMatch = p.match(/^\/v1\/candidates\/([^/]+)\/refs$/);
  if (refMatch) {
    const c = S_CANDIDATES.find((x) => x.candidate_id === refMatch[1]);
    if (!c) return send(res, 404, { ok: false, error: "not_found", detail: "no such candidate" });
    const results = (c.evidence_refs ?? []).map((raw) => {
      const m = String(raw).match(/^([a-z]+):\/\/([^/]+)(?:\/(.*))?$/);
      const ref = {
        scheme: m ? m[1] : "?",
        authority: m ? m[2] : String(raw),
        path: m && m[3] ? m[3].split("/") : [],
      };
      if (ref.scheme === "rawlog") {
        const body = "[mock] " + ref.authority + "\n2026-08-13T10:03:12Z motor overheat 96C\n";
        return {
          ok: true, source: "rawlog", ref,
          payload: {
            event_id: ref.authority,
            key: "rawlog/" + ref.authority + ".log",
            source: "file",
            content: body,
            content_sha256: "0".repeat(64),
            bytes: body.length,
            truncated: false,
          },
        };
      }
      if (ref.scheme === "metricwin") {
        return {
          ok: true, source: "metricwin", ref,
          payload: {
            candidate_id: ref.authority,
            indexed: true,
            component_ref: c.component_ref,
            window: { start: c.window_start, end: c.window_end },
            evidence: c.evidence,
          },
        };
      }
      return { ok: false, ref, error: "invalid_input", detail: "mock 只实现了 metricwin/rawlog" };
    });
    return send(res, 200, {
      ok: true,
      candidate_id: c.candidate_id,
      results,
      resolved: results.filter((r) => r.ok).length,
      total: results.length,
    });
  }

  const evMatch = p.match(/^\/v1\/candidates\/([^/]+)\/evidence$/);
  if (evMatch) {
    const c = S_CANDIDATES.find((x) => x.candidate_id === evMatch[1]);
    if (!c) return send(res, 404, { ok: false, error: "not_found" });
    return send(res, 200, {
      ok: true,
      candidate_id: c.candidate_id,
      device_sn: c.device_sn,
      component_ref: c.component_ref,
      window: { start: c.window_start, end: c.window_end },
      metrics: Object.fromEntries(c.evidence.map((e) => [e.key, e.to_value])),
      reference_window_metrics: Object.fromEntries(
        c.evidence.filter((e) => e.baseline != null).map((e) => [e.key, e.baseline]),
      ),
      evidence: c.evidence,
      vendor_knowledge: [
        { source: "motor_msg_GO-M8010-6.h", line: 42, text: "温度保护阈值 90°C", verified: true },
      ],
    });
  }

  if (p === "/v1/alerts") {
    let items = S_ALERTS;
    if (q.get("state")) items = items.filter((a) => a.state === q.get("state"));
    return send(res, 200, page(items, limit, offset));
  }
  if (p === "/v1/outbox") {
    let items = S_OUTBOX;
    if (q.get("status")) items = items.filter((o) => o.status === q.get("status"));
    return send(res, 200, page(items, limit, offset));
  }
  if (p === "/v1/reports") return send(res, 200, page(S_REPORTS, limit, offset));

  // ---- 异步 job 三件套（ASYNC=1 才有意义，否则 404）----
  const jobResult = p.match(/^\/v1\/investigations\/jobs\/([^/]+)\/result$/);
  if (jobResult && req.method === "GET") {
    const job = JOBS.get(jobResult[1]);
    if (!job) return send(res, 404, { ok: false, error: "not_found", detail: "no such job" });
    if (job.status !== "succeeded")
      return send(res, 409, { ok: false, error: "ambiguous", detail: "job not succeeded: " + job.status });
    return send(res, 200, {
      ...INVESTIGATION,
      candidate_id: job.candidate_id,
      report: { ...INVESTIGATION.report, candidate_refs: [job.candidate_id] },
    });
  }
  const jobCancel = p.match(/^\/v1\/investigations\/jobs\/([^/]+)\/cancel$/);
  if (jobCancel && req.method === "POST") {
    const job = JOBS.get(jobCancel[1]);
    if (!job) return send(res, 404, { ok: false, error: "not_found", detail: "no such job" });
    job.status = "cancelled";
    return send(res, 200, { ok: true, job: jobPublic(job) });
  }
  const jobOne = p.match(/^\/v1\/investigations\/jobs\/([^/]+)$/);
  if (jobOne && req.method === "GET") {
    const job = JOBS.get(jobOne[1]);
    if (!job) return send(res, 404, { ok: false, error: "not_found", detail: "no such job" });
    if (job.status === "running") {
      job.polls += 1;
      // 头两次轮询仍报 running：让客户端真的走一轮轮询，而不是一次就拿到结果
      // （一次就拿到 = 异步路径根本没被走到，测试会假绿）。
      if (job.polls >= 2) {
        job.status = "succeeded";
        job.report_id = INVESTIGATION.report.investigation_id;
      }
    }
    return send(res, 200, { ok: true, job: jobPublic(job) });
  }

  if (p === "/v1/investigations" && req.method === "POST") {
    const body = await readJson(req);
    const candidateId = typeof body.candidate_id === "string" && body.candidate_id
      ? body.candidate_id
      : INVESTIGATION.candidate_id;
    if (ASYNC) {
      jobSeq += 1;
      const jobId = "job_mock_" + jobSeq;
      JOBS.set(jobId, {
        job_id: jobId,
        candidate_id: candidateId,
        status: "running",
        report_id: null,
        error_code: null,
        polls: 0,
      });
      return send(res, 202, { ok: true, job_id: jobId });
    }
    // 模拟同步长任务：延迟 1.5s 再返回，让进度轨可见
    return setTimeout(() => send(res, 200, INVESTIGATION), 1500);
  }

  return send(res, 404, { ok: false, error: "not_found", detail: `${p} not implemented in mock` });
});

server.listen(PORT, () => {
  console.log(`[mock] data_dog mock backend on http://localhost:${PORT}  (tenant=${TENANT})`);
  console.log(`[mock] mode=${EMPTY ? "EMPTY (all lists empty)" : "DEMO"} investigate=${ASYNC ? "ASYNC (poll)" : "SYNC"}`);
  console.log(`[mock] candidates=${S_CANDIDATES.length} alerts=${S_ALERTS.length} reports=${S_REPORTS.length} outbox=${S_OUTBOX.length}`);
});
