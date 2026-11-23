"""Build the IM-07 continuous production-line agent and save it to the canvas.

The scenario is an injection-moulding line producing a PP battery tray, exposed
through a gateway shaped like EUROMAP 77 / MES integration. The workflow runs one
bounded control cycle per invocation; a minutely schedule makes it continuous.
That split is deliberate: a single graph run that never ends would spend the
runtime budget forever and could not be stopped or audited. In production the
same split exists between the PLC's scan loop and the MES job that polls it.

Architecture:
  Start -> read line snapshot -> append telemetry to results.db -> read history
        -> daily-report branch OR anomaly branch
  anomaly -> deterministic diagnosis + safety envelope check
          -> safe: write setpoints through the guarded gateway -> audit -> LLM
             operator note -> email
          -> unsafe: audit escalation -> LLM escalation note -> email
  daily   -> data analysis -> LLM shift report -> audit -> email

The model writes the human-facing diagnosis/report. The setpoint decision and
its safety envelope are deterministic code, because a model must not be the last
line of defence on a physical line.

Run:
    python -m scripts.build_im07_workflow [--save]
"""

from __future__ import annotations

import argparse
import json

from app.db.repository import save_workflow
from app.graph.layout import layout_graph
from app.graph.schema import EdgeSpec, NodeSpec, WorkflowGraph
from app.graph.validate import validate_graph

WORKFLOW_ID = "im07-line-guardian"
LINE_BASE_URL = "http://127.0.0.1:8101"
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 8025
FROM_ADDR = "line-agent@im07.local"
TO_ADDR = "shift-supervisor@im07.local"


def node(
    node_id: str,
    node_type: str,
    label: str,
    config: dict,
    *,
    description: str = "",
    trusted: bool = False,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=node_type,
        label=label,
        description=description,
        config=config,
        trusted=trusted,
    )


def edge(source: str, target: str, source_handle: str | None = None) -> EdgeSpec:
    return EdgeSpec(
        id=f"e-{source}-{target}-{source_handle or 'out'}",
        source=source,
        sourceHandle=source_handle,
        target=target,
        targetHandle="in",
        label="",
    )


def api_auth() -> dict:
    return {
        "auth_kind": "api_key",
        "api_key_name": "X-Line-Key",
        "api_key_value": "env:LINE_API_KEY",
        "api_key_location": "header",
    }


DIAGNOSE_CODE = '''\
state = vars.get("line_state", {}).get("out", {}) or {}
telemetry = state.get("telemetry", {}) or {}
setpoints = state.get("setpoints", {}) or {}
envelope = state.get("envelope", {}) or {}
history = vars.get("history", {}).get("out", {}) or {}
rows = history.get("rows", [])


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


shot = num(telemetry.get("shot_weight_g"))
scrap = num(telemetry.get("scrap_rate_pct"))
oee = num(telemetry.get("oee_pct"))
hold_actual = num(telemetry.get("hold_pressure_bar"))
hold_set = num(setpoints.get("hold_pressure_bar"))
melt_actual = num(telemetry.get("melt_temp_c"))
melt_set = num(setpoints.get("melt_temp_c"))
alarm = (state.get("primary_alarm") or {}).get("code", "")

recent = []
for row in rows[:10]:
    recent.append(num(row.get("scrap_rate_pct")))
avg_recent_scrap = sum(recent) / len(recent) if recent else scrap

proposed = {}
diagnosis = ""
if alarm == "E-311" or shot < 127.0 or scrap >= 3.0:
    hold_limit = envelope.get("hold_pressure_bar", {}) or {}
    melt_limit = envelope.get("melt_temp_c", {}) or {}
    new_hold = min(hold_set + 15.0, num(hold_limit.get("max")))
    new_melt = min(melt_set + 2.0, num(melt_limit.get("max")))
    proposed["hold_pressure_bar"] = round(new_hold, 1)
    proposed["melt_temp_c"] = round(new_melt, 1)
    diagnosis = (
        "E-311 短射趋势：单件重量 %.2f g（下限 127.0 g），废品率 %.2f%%，"
        "近 10 个周期平均废品率 %.2f%%；实测保压比设定低 %.1f bar，料温比设定高 %.1f C。"
        "按工艺窗口提高保压至 %.1f bar、料温至 %.1f C。"
        % (
            shot,
            scrap,
            avg_recent_scrap,
            hold_set - hold_actual,
            melt_actual - melt_set,
            new_hold,
            new_melt,
        )
    )
elif alarm:
    diagnosis = "检测到未纳入自动处置的报警 %s，需要人工确认。" % alarm

safe = bool(proposed)
for key in proposed:
    limit = envelope.get(key, {}) or {}
    value = proposed[key]
    current = num(setpoints.get(key))
    if value < num(limit.get("min")) or value > num(limit.get("max")):
        safe = False
    if abs(value - current) > num(limit.get("max_step")) + 0.0001:
        safe = False

result = {
    "diagnosis": diagnosis,
    "proposed_changes": proposed,
    "safe": safe,
    "alarm_code": alarm,
    "metrics": {
        "shot_weight_g": shot,
        "scrap_rate_pct": scrap,
        "oee_pct": oee,
        "hold_pressure_actual_bar": hold_actual,
        "hold_pressure_set_bar": hold_set,
        "melt_temp_actual_c": melt_actual,
        "melt_temp_set_c": melt_set,
        "avg_recent_scrap_pct": round(avg_recent_scrap, 2),
    },
}
'''

COOLDOWN_CODE = '''\nstate = vars.get("line_state", {}).get("out", {}) or {}
history = vars.get("last_action", {}).get("out", {}) or {}
rows = history.get("rows", [])


def minutes_since(now_text, then_text):
    try:
        from datetime import datetime

        now = datetime.fromisoformat(str(now_text))
        then = datetime.fromisoformat(str(then_text))
        return (now - then).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 0.0


last = rows[0] if rows else {}
minutes = minutes_since(state.get("timestamp", ""), last.get("at", "")) if last else 9999.0
last_action = last.get("action", "") if last else ""
# A rejected change waits longer: retrying an escalation every minute is noise.
# An accepted change gets a short observation window to show its effect.
window = 5.0 if last_action == "auto_setpoint_change" else 30.0
result = {
    "suppress": bool(last) and minutes < window,
    "minutes_since_last": round(minutes, 1) if last else 9999.0,
    "last_action": last_action,
    "cooldown_minutes": window,
    "alarm_code": (state.get("primary_alarm") or {}).get("code", ""),
}
'''

DAILY_STATS_CODE = '''\
history = vars.get("history", {}).get("out", {}) or {}
rows = history.get("rows", [])


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


scrap_values = []
oee_values = []
alarm_samples = 0
phases = {}
for row in rows:
    scrap_values.append(num(row.get("scrap_rate_pct")))
    oee_values.append(num(row.get("oee_pct")))
    if num(row.get("alarm_count")) > 0:
        alarm_samples += 1
    phase = row.get("phase") or "unknown"
    phases[phase] = phases.get(phase, 0) + 1

samples = len(rows)
avg_scrap = sum(scrap_values) / samples if samples else 0.0
avg_oee = sum(oee_values) / samples if samples else 0.0
peak_scrap = max(scrap_values) if scrap_values else 0.0

result = {
    "samples": samples,
    "avg_scrap_pct": round(avg_scrap, 2),
    "peak_scrap_pct": round(peak_scrap, 2),
    "avg_oee_pct": round(avg_oee, 2),
    "alarm_samples": alarm_samples,
    "phase_counts": phases,
}
'''

LLM_SYSTEM = (
    "你是注塑产线 IM-07 的班次助理，面向车间主管写中文简报。"
    "只能使用给定数据，不得编造产量、温度或根因。"
    "语气直接、可执行，给出下一步人工确认点。不要使用 Markdown 标题。"
)

FIX_PROMPT = """\
产线 IM-07 出现异常并已执行受控调参。请写一段 120-200 字的中文处置说明，包含：
1) 报警与关键证据（单件重量、废品率、保压/料温偏差）；
2) 已采取的参数变更及理由；
3) 预期效果与下一轮监控要看的指标；
4) 如果 3 个周期内未恢复，需要人工做什么。

当前快照：
{{ vars.line_state.out }}

诊断与变更：
{{ vars.diagnose.out }}

网关确认：
{{ vars.apply_setpoints.out }}
"""

ESCALATE_PROMPT = """\
产线 IM-07 出现异常，但自动调参被安全护栏拒绝或没有可用方案。请写一段 150-220 字的中文升级说明，包含：
1) 报警与关键证据；
2) 为什么没有自动改参数（超工艺窗口、步长过大或缺少规则）；
3) 建议人工检查的项目；
4) 在人工到场前建议的降级措施（如降速、隔离批次）。

当前快照：
{{ vars.line_state.out }}

诊断：
{{ vars.diagnose.out }}
"""

DAILY_PROMPT = """\
请写一份 200-300 字的中文班次生产简报，包含：数据样本数、平均/峰值废品率、
平均 OEE、报警样本数、主要异常阶段与已采取的动作，以及下一班建议。
数字必须来自下面的统计与最近记录。

统计：
{{ vars.daily_stats.out }}

最近记录：
{{ vars.history.out }}

当前快照：
{{ vars.line_state.out }}
"""


def build_graph() -> WorkflowGraph:
    nodes = [
        node(
            "start",
            "start",
            "班次/监控入口",
            {
                "input_schema": {"line_id": "IM-07", "mode": "monitor"},
                "note": (
                    "mode=monitor 每分钟执行一次控制循环；"
                    "mode=daily_report 由每日 08:00 UTC 的计划触发。"
                ),
            },
            description="入口：声明产线编号和本次是监控还是日报。",
        ),
        node(
            "line_state",
            "api_call",
            "读取产线快照",
            {
                "url": LINE_BASE_URL + "/line/state",
                "method": "GET",
                **api_auth(),
                "timeout": 15,
                "fail_on_error": True,
            },
            description=(
                "通过 MES 网关读取当前周期的温度、压力、重量、OEE、报警和工艺窗口。"
                "真实产线对应 EUROMAP 77 OPC UA 读节点。"
            ),
        ),
        node(
            "write_telemetry",
            "tool",
            "记录遥测",
            {
                "tool": "db_write",
                "arguments": {
                    "table": "line_telemetry",
                    "rows": [
                        {
                            "line_id": "{{ vars.start.out.line_id }}",
                            "cycle": "{{ vars.line_state.out.cycle }}",
                            "phase": "{{ vars.line_state.out.phase }}",
                            "state": "{{ vars.line_state.out.state }}",
                            "melt_temp_c": "{{ vars.line_state.out.telemetry.melt_temp_c }}",
                            "hold_pressure_bar": "{{ vars.line_state.out.telemetry.hold_pressure_bar }}",
                            "injection_pressure_bar": "{{ vars.line_state.out.telemetry.injection_pressure_bar }}",
                            "cycle_time_s": "{{ vars.line_state.out.telemetry.cycle_time_s }}",
                            "shot_weight_g": "{{ vars.line_state.out.telemetry.shot_weight_g }}",
                            "scrap_rate_pct": "{{ vars.line_state.out.telemetry.scrap_rate_pct }}",
                            "oee_pct": "{{ vars.line_state.out.telemetry.oee_pct }}",
                            "alarm_count": "{{ vars.line_state.out.alarm_count }}",
                            "primary_alarm": "{{ vars.line_state.out.primary_alarm.code }}",
                            "snapshot_at": "{{ vars.line_state.out.timestamp }}",
                        }
                    ],
                },
                "fail_on_error": True,
            },
            description="把每个周期的遥测追加到 results.db，形成可分析的历史。",
        ),
        node(
            "history",
            "tool",
            "读取近期趋势",
            {
                "tool": "db_query",
                "arguments": {"table": "line_telemetry", "limit": 20},
                "fail_on_error": True,
            },
            description="读取最近 20 个周期，供趋势判断和班次汇总。",
        ),
        node(
            "mode_gate",
            "condition",
            "监控还是日报",
            {
                "mode": "first_match",
                "branches": [
                    {
                        "id": "daily",
                        "label": "生成班次日报",
                        "expression": "vars.start.out.mode == 'daily_report'",
                    },
                    {
                        "id": "monitor",
                        "label": "执行监控周期",
                        "expression": "",
                    },
                ],
            },
            description="日报和分钟级监控共用一套取数与入库节点。",
        ),
        node(
            "monitor_gate",
            "condition",
            "是否异常",
            {
                "mode": "first_match",
                "branches": [
                    {
                        "id": "anomaly",
                        "label": "异常：报警/重量/废品率",
                        "expression": (
                            "vars.line_state.out.alarm_count > 0 "
                            "or vars.line_state.out.telemetry.scrap_rate_pct >= 3.0 "
                            "or vars.line_state.out.telemetry.shot_weight_g < 127.0"
                        ),
                    },
                    {"id": "normal", "label": "正常", "expression": ""},
                ],
            },
            description=(
                "异常判据来自工艺窗口：有报警、废品率≥3% 或单件重量低于 127 g。"
            ),
        ),
        node(
            "last_action",
            "tool",
            "查询上次处置",
            {
                "tool": "db_query",
                "arguments": {
                    "table": "line_actions",
                    "limit": 1,
                    "where_column": "alarm_code",
                    "where_value": "{{ vars.line_state.out.primary_alarm.code }}",
                },
                "fail_on_error": False,
            },
            description="同一报警 30 分钟内只处置一次，避免每分钟重复邮件。",
        ),
        node(
            "cooldown",
            "code",
            "告警抑制判断",
            {"code": COOLDOWN_CODE, "fail_on_error": True},
            description="用最近一次处置时间做冷却判断；冷却中只记录遥测，不重复调参或发邮件。",
            trusted=True,
        ),
        node(
            "cooldown_gate",
            "condition",
            "是否已冷却",
            {
                "mode": "first_match",
                "branches": [
                    {
                        "id": "proceed",
                        "label": "可以处置",
                        "expression": "not vars.cooldown.out.suppress",
                    },
                    {"id": "suppressed", "label": "冷却中", "expression": ""},
                ],
            },
            description="冷却窗口内的同一报警不再触发写入或邮件。",
        ),
        node(
            "end_suppressed",
            "end",
            "完成（冷却中）",
            {"output_label": "result"},
        ),
        node(
            "diagnose",
            "code",
            "规则诊断与安全校验",
            {"code": DIAGNOSE_CODE, "fail_on_error": True},
            description=(
                "用报警码、实测/设定偏差和近期废品率给出候选调参；"
                "按工艺窗口的 min/max 和单次最大步长做确定性校验。"
            ),
            trusted=True,
        ),
        node(
            "safety_gate",
            "condition",
            "自动调参是否安全",
            {
                "mode": "strict",
                "branches": [
                    {
                        "id": "fix",
                        "label": "窗口内：自动执行",
                        "expression": (
                            "vars.diagnose.out.safe and "
                            "len(vars.diagnose.out.proposed_changes) > 0"
                        ),
                    },
                    {
                        "id": "escalate",
                        "label": "拒绝：升级人工",
                        "expression": "",
                    },
                ],
            },
            description="只有规则校验通过且确有变更时才允许写产线。",
        ),
        node(
            "apply_setpoints",
            "api_call",
            "受控写入产线参数",
            {
                "url": LINE_BASE_URL + "/line/setpoints",
                "method": "POST",
                **api_auth(),
                "body": {
                    "line_id": "{{ vars.start.out.line_id }}",
                    "changes": {
                        "hold_pressure_bar": "{{ vars.diagnose.out.proposed_changes.hold_pressure_bar }}",
                        "melt_temp_c": "{{ vars.diagnose.out.proposed_changes.melt_temp_c }}",
                    },
                    "reason": "{{ vars.diagnose.out.diagnosis }}",
                    "actor": "im07-line-guardian",
                    "run_id": "{{ vars.start.out.line_id }}-{{ vars.line_state.out.cycle }}",
                },
                "timeout": 15,
                "fail_on_error": True,
            },
            description=(
                "通过网关写回设定值。网关本身再次校验工艺窗口、步长和变更频率，"
                "并保留审计记录；写入失败会让本次运行失败，不会静默忽略。"
            ),
        ),
        node(
            "write_fix",
            "tool",
            "记录调参审计",
            {
                "tool": "db_write",
                "arguments": {
                    "table": "line_actions",
                    "rows": [
                        {
                            "line_id": "{{ vars.start.out.line_id }}",
                            "cycle": "{{ vars.line_state.out.cycle }}",
                            "action": "auto_setpoint_change",
                            "accepted": "true",
                            "alarm_code": "{{ vars.diagnose.out.alarm_code }}",
                            "diagnosis": "{{ vars.diagnose.out.diagnosis }}",
                            "applied": "{{ vars.apply_setpoints.out.setpoints }}",
                            "gateway_phase": "{{ vars.apply_setpoints.out.phase }}",
                            "at": "{{ vars.line_state.out.timestamp }}",
                        }
                    ],
                },
                "fail_on_error": True,
            },
            description="把这次自动调参的原因、前后值和网关确认写入结果库。",
        ),
        node(
            "notify_fix",
            "llm",
            "生成处置说明",
            {
                "system_prompt": LLM_SYSTEM,
                "prompt": FIX_PROMPT,
                "output_format": "text",
                "temperature": 0.2,
                "max_tokens": 700,
            },
            description="模型只负责把证据和动作解释给主管，不参与安全判定。",
        ),
        node(
            "email_fix",
            "email_send",
            "邮件通知已处置",
            {
                "smtp_host": SMTP_HOST,
                "smtp_port": SMTP_PORT,
                "security": "none",
                "from_addr": FROM_ADDR,
                "to": TO_ADDR,
                "subject": "IM-07 自动处置 · cycle {{ vars.line_state.out.cycle }} · {{ vars.diagnose.out.alarm_code }}",
                "body": "{{ vars.notify_fix.out }}",
                "fail_on_error": False,
            },
            description="内部中继投递；失败不阻断审计记录，但会在运行日志里保留错误。",
        ),
        node(
            "end_fix",
            "end",
            "完成（已调参）",
            {"output_label": "result"},
        ),
        node(
            "write_escalation",
            "tool",
            "记录升级审计",
            {
                "tool": "db_write",
                "arguments": {
                    "table": "line_actions",
                    "rows": [
                        {
                            "line_id": "{{ vars.start.out.line_id }}",
                            "cycle": "{{ vars.line_state.out.cycle }}",
                            "action": "manual_escalation",
                            "accepted": "false",
                            "alarm_code": "{{ vars.diagnose.out.alarm_code }}",
                            "diagnosis": "{{ vars.diagnose.out.diagnosis }}",
                            "applied": "none",
                            "gateway_phase": "{{ vars.line_state.out.phase }}",
                            "at": "{{ vars.line_state.out.timestamp }}",
                        }
                    ],
                },
                "fail_on_error": True,
            },
            description="自动调参被拒绝时也要留下可追溯记录。",
        ),
        node(
            "notify_escalation",
            "llm",
            "生成升级说明",
            {
                "system_prompt": LLM_SYSTEM,
                "prompt": ESCALATE_PROMPT,
                "output_format": "text",
                "temperature": 0.2,
                "max_tokens": 700,
            },
            description="说明为何没有自动改参数，以及人工检查清单。",
        ),
        node(
            "email_escalation",
            "email_send",
            "邮件升级人工",
            {
                "smtp_host": SMTP_HOST,
                "smtp_port": SMTP_PORT,
                "security": "none",
                "from_addr": FROM_ADDR,
                "to": TO_ADDR,
                "subject": "IM-07 需要人工确认 · cycle {{ vars.line_state.out.cycle }} · {{ vars.diagnose.out.alarm_code }}",
                "body": "{{ vars.notify_escalation.out }}",
                "fail_on_error": False,
            },
            description="升级邮件；网关未收到任何调参请求。",
        ),
        node(
            "end_escalation",
            "end",
            "完成（已升级）",
            {"output_label": "result"},
        ),
        node(
            "daily_stats",
            "code",
            "班次数据分析",
            {"code": DAILY_STATS_CODE, "fail_on_error": True},
            description="从遥测历史计算样本数、平均/峰值废品率、平均 OEE 和报警样本。",
            trusted=True,
        ),
        node(
            "daily_report",
            "llm",
            "生成班次报告",
            {
                "system_prompt": LLM_SYSTEM,
                "prompt": DAILY_PROMPT,
                "output_format": "text",
                "temperature": 0.2,
                "max_tokens": 900,
            },
            description="只引用统计和最近记录，不编造数字。",
        ),
        node(
            "write_daily",
            "tool",
            "记录日报",
            {
                "tool": "db_write",
                "arguments": {
                    "table": "line_reports",
                    "rows": [
                        {
                            "line_id": "{{ vars.start.out.line_id }}",
                            "report_type": "shift",
                            "samples": "{{ vars.daily_stats.out.samples }}",
                            "avg_scrap_pct": "{{ vars.daily_stats.out.avg_scrap_pct }}",
                            "avg_oee_pct": "{{ vars.daily_stats.out.avg_oee_pct }}",
                            "alarm_samples": "{{ vars.daily_stats.out.alarm_samples }}",
                            "report": "{{ vars.daily_report.out }}",
                            "at": "{{ vars.line_state.out.timestamp }}",
                        }
                    ],
                },
                "fail_on_error": True,
            },
            description="日报正文与统计一并入库，便于追溯。",
        ),
        node(
            "email_daily",
            "email_send",
            "邮件发送日报",
            {
                "smtp_host": SMTP_HOST,
                "smtp_port": SMTP_PORT,
                "security": "none",
                "from_addr": FROM_ADDR,
                "to": TO_ADDR,
                "subject": "IM-07 班次生产简报 · {{ vars.line_state.out.timestamp }}",
                "body": "{{ vars.daily_report.out }}",
                "fail_on_error": False,
            },
            description="班次简报投递。",
        ),
        node(
            "end_daily",
            "end",
            "完成（日报）",
            {"output_label": "result"},
        ),
        node(
            "end_normal",
            "end",
            "完成（正常）",
            {"output_label": "result"},
        ),
    ]

    edges = [
        edge("start", "line_state", "out"),
        edge("line_state", "write_telemetry", "out"),
        edge("write_telemetry", "history", "out"),
        edge("history", "mode_gate", "out"),
        edge("mode_gate", "daily_stats", "daily"),
        edge("mode_gate", "monitor_gate", "monitor"),
        edge("monitor_gate", "last_action", "anomaly"),
        edge("last_action", "cooldown", "out"),
        edge("cooldown", "cooldown_gate", "out"),
        edge("cooldown_gate", "diagnose", "proceed"),
        edge("cooldown_gate", "end_suppressed", "suppressed"),
        edge("monitor_gate", "end_normal", "normal"),
        edge("diagnose", "safety_gate", "out"),
        edge("safety_gate", "apply_setpoints", "fix"),
        edge("safety_gate", "write_escalation", "escalate"),
        edge("apply_setpoints", "write_fix", "out"),
        edge("write_fix", "notify_fix", "out"),
        edge("notify_fix", "email_fix", "out"),
        edge("notify_fix", "end_fix", "out"),
        edge("write_escalation", "notify_escalation", "out"),
        edge("notify_escalation", "email_escalation", "out"),
        edge("notify_escalation", "end_escalation", "out"),
        edge("daily_stats", "daily_report", "out"),
        edge("daily_report", "write_daily", "out"),
        edge("write_daily", "email_daily", "out"),
        edge("daily_report", "end_daily", "out"),
    ]

    graph = WorkflowGraph(
        id=WORKFLOW_ID,
        name="注塑产线 IM-07 持续监控与调参",
        description=(
            "每 1 分钟从 MES 网关读取注塑机遥测，记录入库并做趋势判断；"
            "异常时按工艺窗口规则诊断，安全范围内自动调参并邮件汇报，"
            "越界则升级人工。每天 08:00 UTC 生成班次报告。"
        ),
        nodes=nodes,
        edges=edges,
    )
    return layout_graph(graph)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true", help="Persist to data/workflows")
    args = parser.parse_args()

    graph = build_graph()
    report = validate_graph(graph)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if not report.ok:
        return 1
    if args.save:
        saved = save_workflow(graph)
        print(f"saved {saved.id}: {saved.name}")
    else:
        print(f"built {graph.id}; pass --save to persist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
