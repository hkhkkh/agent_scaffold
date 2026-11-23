import json
from app.graph.schema import WorkflowGraph, NodeSpec, EdgeSpec, Position
from app.graph.validate import validate_graph
from app.db.repository import save_workflow

CWE_TOOL_CODE = """\"\"\"CWE vulnerability knowledge base lookup tool.\"\"\"
from __future__ import annotations
from typing import Any
from app.tools.registry import ToolDef, register_tool

async def _cwe_lookup(cwe_id: str = "") -> dict[str, Any]:
    db = {
        "CWE-89": {"name": "SQL Injection", "severity": "Critical", "description": "Improper neutralization of special elements used in an SQL command."},
        "CWE-79": {"name": "Cross-Site Scripting", "severity": "High", "description": "Improper neutralization of input during web page generation."},
        "CWE-22": {"name": "Path Traversal", "severity": "High", "description": "Improper limitation of a pathname to a restricted directory."}
    }
    return db.get(cwe_id.strip().upper(), {"name": "General Vulnerability", "severity": "Medium", "description": "Unclassified security issue."})

register_tool(
    ToolDef(
        name="cwe_lookup",
        label="CWE Lookup",
        label_zh="CWE知识库查询",
        description="Query vulnerability severity and CWE definition by ID (e.g. 'CWE-89').",
        parameters={
            "type": "object",
            "properties": {"cwe_id": {"type": "string", "description": "CWE ID such as CWE-89"}},
            "required": ["cwe_id"]
        },
        fn=_cwe_lookup,
    )
)
"""

nodes = [
    NodeSpec(
        id='start_node',
        type='start',
        label='开始：接收代码与参数',
        description='声明输入：待审计代码片段(code_snippet)、审计深度(audit_level)、业务系统名称(system_name)',
        position=Position(x=50, y=240),
        config={
            'input_schema': {
                'code_snippet': {
                    'type': 'string',
                    'default': "def execute_user_query(req):\n    query = f\"SELECT * FROM users WHERE id = '{req['id']}'\"\n    return db.execute(query)"
                },
                'audit_level': {'type': 'string', 'default': 'strict'},
                'system_name': {'type': 'string', 'default': 'UserAuth'}
            }
        }
    ),
    NodeSpec(
        id='mem_recall',
        type='memory',
        label='记忆检索：历史相似漏洞与规范',
        description='从长期记忆中检索有关 SQL 注入、权限控制及安全编码的历史修复规范',
        position=Position(x=380, y=240),
        config={
            'mode': 'search',
            'query': 'SQL 注入 权限控制 安全编码规范',
            'limit': 3,
            'namespace': 'security_guidelines'
        }
    ),
    NodeSpec(
        id='goal_plan',
        type='goal',
        label='目标规划：初始化多步骤审计目标',
        description='为长任务创建可追踪的目标规划：1.静态特征分析 2.智能体综合审计 3.安全风险决策 4.生成重构方案 5.经验沉淀',
        position=Position(x=710, y=240),
        config={
            'mode': 'start',
            'objective': '完成自主代码安全审计与防御性重构',
            'items': [
                '静态特征与复杂度分析',
                '智能体多工具漏洞检测',
                '安全风险分流决策',
                '生成防御性重构方案',
                '经验自动沉淀至长期记忆'
            ],
            'scope': '{run}'
        }
    ),
    NodeSpec(
        id='ast_analyze',
        type='code',
        label='代码分析：敏感特征与风险度量',
        description='使用 Python 沙盒分析代码的字符行数、SQL关键词及敏感字符串模式',
        position=Position(x=1040, y=240),
        trusted=True,
        config={
            'code': (
                "code_text = str(vars.get('start_node', {}).get('out', {}).get('code_snippet') or inputs.get('in') or '')\n"
                "lines = len(code_text.splitlines())\n"
                "has_sql = any(k in code_text.upper() for k in ['SELECT ', 'UPDATE ', 'DELETE ', 'INSERT '])\n"
                "has_format = 'f\"' in code_text or '.format(' in code_text or '%' in code_text\n"
                "risk_score = 90 if (has_sql and has_format) else 20\n"
                "result = {\n"
                "    'lines': lines,\n"
                "    'has_sql': has_sql,\n"
                "    'has_format': has_format,\n"
                "    'risk_score': risk_score,\n"
                "    'finding': '检测到动态拼接 SQL 查询，存在严重注入隐患' if risk_score >= 80 else '未检测到明显高危漏洞'\n"
                "}\n"
            )
        }
    ),
    NodeSpec(
        id='agent_audit',
        type='agent',
        label='智能体：自主漏洞推理与综合研判',
        description='智能体根据代码特征、检索出的记忆知识库，自主调用内置工具进行综合安全研判',
        position=Position(x=1370, y=240),
        config={
            'system_prompt': '你是一位资深安全架构师。请针对输入的代码分析漏洞，如有需要可通过 cwe_lookup 工具查询官方缺陷定义，并给出明确的风险定级与防御建议。',
            'task': (
                "待审计系统：{{ vars.start_node.out.system_name }}\n"
                "待审计代码：\n{{ vars.start_node.out.code_snippet }}\n\n"
                "初步分析特征：\n{{ vars.ast_analyze.out.finding }} (风险分数: {{ vars.ast_analyze.out.risk_score }})\n\n"
                "历史安全经验：\n{{ vars.mem_recall.out }}\n\n"
                "请运用安全审计专业经验，综合研判其漏洞成因、危害等级并给出防御建议。"
            ),
            'tools': [],
            'max_iterations': 5
        }
    ),
    NodeSpec(
        id='risk_branch',
        type='condition',
        label='条件分支：风险级别分流决策',
        description='若静态分析与智能体判定风险分 >= 80，走高危通道强制人工复核；否则走普通自动化通道',
        position=Position(x=1700, y=240),
        config={
            'branches': [
                {'id': 'yes', 'label': '高危分支 (需审批)', 'expression': 'vars.ast_analyze.out.risk_score >= 80'},
                {'id': 'no', 'label': '低危分支 (自动通过)', 'expression': ''}
            ],
            'mode': 'first_match'
        }
    ),
    NodeSpec(
        id='human_approval',
        type='human_input',
        label='人工介入：高危漏洞修复审批',
        description='暂停流程，将智能体研判出的高危漏洞及初步修复建议呈现给安全管理员进行审批',
        position=Position(x=2030, y=100),
        config={
            'message': '系统检测到高危安全隐患：{{ vars.ast_analyze.out.finding }} (风险分: {{ vars.ast_analyze.out.risk_score }})。请安全管理员核实是否允许自动应用参数化重构方案。',
            'allow_reject': True,
            'show_context': True
        }
    ),
    NodeSpec(
        id='gate_merge',
        type='aggregator',
        label='分支汇聚：审批结果与自动通道汇合',
        description='汇总高危审批结果与低危通道数据，统一送往方案重构节点',
        position=Position(x=2360, y=240),
        config={
            'mode': 'first_non_null'
        }
    ),
    NodeSpec(
        id='remedy_generator',
        type='llm',
        label='大模型：生成防御性重构方案',
        description='根据审计研判结论与行业最佳实践，生成参数化查询重构代码与加固说明文档',
        position=Position(x=2690, y=240),
        config={
            'system_prompt': '你是一位安全重构专家。针对发现的漏洞，请给出标准的企业级安全加固后代码，并说明修改要点。',
            'prompt': (
                "原始代码：\n{{ vars.start_node.out.code_snippet }}\n\n"
                "智能体研判结论：\n{{ vars.agent_audit.out }}\n\n"
                "请给出完整的参数化查询重构代码、修改对比说明以及加固建议。"
            )
        }
    ),
    NodeSpec(
        id='mem_persist',
        type='memory',
        label='记忆沉淀：将优秀修复模式入库',
        description='将本次审计提炼出的有效漏洞防护模式自动记录到长期记忆库，供后续任务检索复用',
        position=Position(x=3020, y=240),
        config={
            'mode': 'write',
            'namespace': 'security_guidelines',
            'key': 'sql_injection_defense_{{ vars.start_node.out.system_name }}',
            'text': '针对动态 SQL 拼接漏洞，强制采用参数化绑定变量方案。修复方案参考：{{ vars.remedy_generator.out }}'
        }
    ),
    NodeSpec(
        id='goal_finish',
        type='goal',
        label='目标完成：标记长任务目标结束',
        description='推进目标追踪清单，将任务全部标记为已完成',
        position=Position(x=3350, y=240),
        config={
            'mode': 'advance',
            'scope': '{run}',
            'item': '5',
            'item_status': 'completed',
            'goal_status': 'done'
        }
    ),
    NodeSpec(
        id='end_node',
        type='end',
        label='结束：交付全流程审计与重构报告',
        description='汇总审计发现、重构后代码、人机协同记录与记忆沉淀日志，作为工作流最终产出物',
        position=Position(x=3680, y=240),
        config={
            'output_label': 'audit_and_remediation_report',
            'include_state': True
        }
    )
]

edges = [
    EdgeSpec(id='e1', source='start_node', source_handle='out', target='mem_recall', target_handle='in'),
    EdgeSpec(id='e2', source='mem_recall', source_handle='out', target='goal_plan', target_handle='in'),
    EdgeSpec(id='e3', source='goal_plan', source_handle='out', target='ast_analyze', target_handle='in'),
    EdgeSpec(id='e4', source='ast_analyze', source_handle='out', target='agent_audit', target_handle='in'),
    EdgeSpec(id='e5', source='agent_audit', source_handle='out', target='risk_branch', target_handle='in'),
    EdgeSpec(id='e6_high', source='risk_branch', source_handle='yes', target='human_approval', target_handle='in'),
    EdgeSpec(id='e6_low', source='risk_branch', source_handle='no', target='gate_merge', target_handle='in'),
    EdgeSpec(id='e7_human', source='human_approval', source_handle='out', target='gate_merge', target_handle='in'),
    EdgeSpec(id='e8', source='gate_merge', source_handle='out', target='remedy_generator', target_handle='in'),
    EdgeSpec(id='e9', source='remedy_generator', source_handle='out', target='mem_persist', target_handle='in'),
    EdgeSpec(id='e10', source='mem_persist', source_handle='out', target='goal_finish', target_handle='in'),
    EdgeSpec(id='e11', source='goal_finish', source_handle='out', target='end_node', target_handle='in')
]

wf = WorkflowGraph(
    id='wf-security-remediation-expert',
    name='自主代码安全审计与重构智能体',
    description='全流程自主 Agent：结合长期记忆经验检索、目标步骤规划、Python AST度量、智能体多工具研判、高危人工审批阻断、大模型加固重构与记忆自沉淀。',
    nodes=nodes,
    edges=edges,
    tools={'cwe_lookup': CWE_TOOL_CODE}
)

report = validate_graph(wf)
print('VALIDATION OK:', report.ok)
for item in report.issues:
    print(' ', item.severity, item.code, item.message)

if report.ok:
    save_workflow(wf)
    print(f"SUCCESS: Saved workflow '{wf.name}' ({wf.id}) to database!")
