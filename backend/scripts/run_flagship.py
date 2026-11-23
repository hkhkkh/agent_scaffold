import time
import httpx
import json
import sys

# Windows UTF-8 stdout
sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://127.0.0.1:8001"

def run_flagship():
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)
    
    workflow_id = "wf-security-remediation-expert"
    inputs = {
        "code_snippet": "def execute_user_query(req):\n    query = f\"SELECT * FROM users WHERE id = '{req['id']}'\"\n    return db.execute(query)",
        "audit_level": "strict",
        "system_name": "UserAuth"
    }
    
    print(f"=== 1. 发起工作流运行: {workflow_id} ===")
    resp = client.post("/api/runs", json={
        "workflow_id": workflow_id,
        "inputs": inputs
    })
    if resp.status_code != 201:
        print(f"启动失败: {resp.status_code} {resp.text}")
        return
    
    data = resp.json()
    run_id = data["id"]
    print(f"工作流已启动，Run ID: {run_id}, 状态: {data['status']}")
    
    while True:
        r = client.get(f"/api/runs/{run_id}")
        run_data = r.json()
        current_status = run_data.get("status")
        steps = run_data.get("steps", [])
        print(f"当前状态: {current_status} | 已记录步骤数: {len(steps)}")
        
        if current_status == "interrupted":
            interrupt = run_data.get("interrupt", {})
            print("\n" + "="*50)
            print("[INTERRUPT] 触发人机协同阻断 (Human Approval Gate)!")
            print(f"审批消息: {interrupt.get('message')}")
            print("模拟安全管理员签署审批意见: 批准使用参数化绑定重构方案...")
            print("="*50 + "\n")
            
            resume_resp = client.post(f"/api/runs/{run_id}/resume", json={
                "approved": True,
                "review_notes": "安全管理员已审查：同意自动生成参数化加固重构代码"
            })
            print(f"恢复运行响应状态码: {resume_resp.status_code}")
            time.sleep(3)
            continue
            
        elif current_status in ("succeeded", "failed"):
            print("\n" + "="*60)
            print(f"最终执行结果: {current_status.upper()}")
            print("="*60)
            
            steps_resp = client.get(f"/api/runs/{run_id}/steps")
            all_steps = steps_resp.json().get("steps", [])
            for s in all_steps:
                node_label = s.get("node_label") or s.get("node_id")
                dur = s.get("duration_ms", 0)
                status_icon = "[OK]" if s.get("status") == "succeeded" else "[FAIL]"
                print(f"  {status_icon} [{s.get('node_id')}] {node_label} ({dur:.2f}ms)")
                if s.get("status") == "failed":
                    print(f"       ERROR: {s.get('error')}")
            
            outputs = run_data.get("outputs", {})
            print("\n" + "="*60)
            print("最终交付报告与状态输出 (Final Outputs):")
            print("="*60)
            print(json.dumps(outputs, ensure_ascii=False, indent=2))
            break
            
        time.sleep(3)

if __name__ == "__main__":
    run_flagship()
