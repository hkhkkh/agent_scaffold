import json
from app.db.repository import get_workflow, save_workflow
from app.graph.validate import validate_graph

wf = get_workflow("eb209200e0b0")

# 1. 修复连线 handles
# 清理从 condition_4aa8 和 loop_da53 出发的边，重新精确绑定
new_edges = []
for e in wf.edges:
    if e.source == "condition_4aa8":
        continue
    if e.source == "loop_da53":
        continue
    if e.id == "e16-memory_f8d7-end_5bef":
        continue
    new_edges.append(e)

from app.graph.schema import EdgeSpec

# 精确绑定条件分支
# need_info -> human_input_6f78
new_edges.append(EdgeSpec(id="e_cond_info", source="condition_4aa8", source_handle="need_info", target="human_input_6f78", target_handle="in"))
# research -> agent_2697
new_edges.append(EdgeSpec(id="e_cond_research", source="condition_4aa8", source_handle="research", target="agent_2697", target_handle="in"))
# order -> agent_029f
new_edges.append(EdgeSpec(id="e_cond_order", source="condition_4aa8", source_handle="order", target="agent_029f", target_handle="in"))
# fallback -> agent_2697
new_edges.append(EdgeSpec(id="e_cond_fallback", source="condition_4aa8", source_handle="fallback", target="agent_2697", target_handle="in"))

# 精确绑定循环
# continue 回环 -> agent_2697 (继续调研思考)
new_edges.append(EdgeSpec(id="e_loop_continue", source="loop_da53", source_handle="continue", target="agent_2697", target_handle="in"))
# done 退出 -> llm_3fc1 (生成最终对客答复)
new_edges.append(EdgeSpec(id="e_loop_done", source="loop_da53", source_handle="done", target="llm_3fc1", target_handle="in"))

# 精确绑定结束输出：从 llm_3fc1 的 out 直接连接到 end_5bef，确保方案全文交付
new_edges.append(EdgeSpec(id="e_final_output", source="llm_3fc1", source_handle="out", target="end_5bef", target_handle="in"))

wf.edges = new_edges

# 2. 增强节点配置
for node in wf.nodes:
    if node.id == "end_5bef":
        node.config["include_state"] = True
        node.config["output_label"] = "final_ecommerce_solution"
    elif node.id == "human_input_6f78":
        node.config["message"] = "【电商智能体需要您参与确认】\n我们正在为您制定处理方案，但发现信息不足：{{ vars.llm_aae4.out.missing_info }}\n请您在下方补充信息以便继续："
    elif node.id == "agent_2697":
        # 允许工具报错时模型自主调整
        node.config["fail_on_tool_error"] = False

report = validate_graph(wf)
print("VALIDATOR OK:", report.ok)
if not report.ok:
    for e in report.errors:
        print("  Error:", e.code, e.message)
for w in report.warnings:
    print("  Warning:", w.code, w.message)

if report.ok:
    save_workflow(wf)
    print("SUCCESS: 成功修复电商 Agent 的全部端口连线与交付链路！")
