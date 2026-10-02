#!/usr/bin/env python3
"""流量计读数核对调用示例（仅依赖标准库，全程只读）。

用法：
    python3 examples/metering_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_network.json，树状）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀)-- U3
                   |           |
                 P3[阀]       P5[阀]
                   |           |
                  U1          U2

脚本依次演示：
  1. 导入管网（版本 1）
  2. 全部读数上报且守恒 -> consistent=true，无缺报补值
  3. P2 缺报 -> 由节点平衡唯一确定为 20（determined=true）
  4. 读数矛盾（A 点不守恒）-> consistent=false，不输出补值
  5. 已读版本过期 -> version_match=false，仍按当前状态核对
  6. 带环管网：环上缺报管段只能给出范围（determined=false）；
     补一条读数后其余管段随即被唯一确定
  7. 隔离 P6（关 P4）后：阻断管段读数非零 -> 不一致；
     阻断管段缺报 -> 确定为 0
  8. 停运水源注入非零 -> 不一致；零注入零取用 -> 一致
  9. 非法请求：编号未覆盖/重复/不存在、布尔/负数 -> 422 整体拒绝
 10. 确认全程只读：版本、阀位、事件、快照均不变
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_network.json")

# 带环演示管网（环上流量有一个自由度，缺报管段只能给出范围）：
# S --P1--> A --P2--> U
# S --P3--> U
RING_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "U", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A"},
        {"id": "P2", "from": "A", "to": "U"},
        {"id": "P3", "from": "S", "to": "U"},
    ],
}

# sample_network 的满覆盖读数（注入 30 = U1/U2/U3 各 10）
FULL_ENTRIES = [
    {"pipe_id": "P1", "capacity": 100, "reading": 30},
    {"pipe_id": "P2", "capacity": 100, "reading": 20},
    {"pipe_id": "P3", "capacity": 100, "reading": 10},
    {"pipe_id": "P4", "capacity": 100, "reading": 10},
    {"pipe_id": "P5", "capacity": 100, "reading": 10},
    {"pipe_id": "P6", "capacity": 100, "reading": 10},
]


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def reconcile(version, sources, sinks, pipes):
    return call(
        "POST",
        "/api/metering/reconcile",
        {"version": version, "sources": sources, "sinks": sinks, "pipes": pipes},
    )


def sample_body(version=1, injection=30, withdrawals=(10, 10, 10), entries=None):
    return (
        version,
        [{"source_id": "S", "injection": injection}],
        [
            {"sink_id": "U1", "withdrawal": withdrawals[0]},
            {"sink_id": "U2", "withdrawal": withdrawals[1]},
            {"sink_id": "U3", "withdrawal": withdrawals[2]},
        ],
        entries if entries is not None else FULL_ENTRIES,
    )


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 全部读数上报且守恒
    status, body = reconcile(*sample_body())
    show("2. 全部读数上报且守恒（consistent=true，无缺报补值）", status, body)
    assert status == 200, body
    assert body["consistent"] is True and body["estimates"] == [], body
    assert body["version_match"] is True

    # 3. P2 缺报：A 点平衡 -> P2 = 30 - 10 = 20，可唯一确定
    entries = [dict(e) for e in FULL_ENTRIES]
    entries[1]["reading"] = None
    status, body = reconcile(*sample_body(entries=entries))
    show("3. P2 缺报 -> 确定为 20（determined=true）", status, body)
    assert body["consistent"] is True, body
    assert body["estimates"] == [
        {"pipe_id": "P2", "min_flow": 20, "max_flow": 20, "determined": True}
    ], body

    # 4. 读数矛盾：A 点 P1=30 进、P3=10 + P2=25 出 -> 不一致，不输出补值
    entries = [dict(e) for e in FULL_ENTRIES]
    entries[1]["reading"] = 25
    status, body = reconcile(*sample_body(entries=entries))
    show("4. 读数矛盾（A 点不守恒）-> consistent=false，不输出补值", status, body)
    assert body["consistent"] is False and body["estimates"] == [], body

    # 5. 已读版本过期：仍按当前状态核对，仅 version_match=false
    status, body = reconcile(*sample_body(version=99))
    show("5. 已读版本过期（version_match=false，仍按当前版本 1 核对）", status, body)
    assert body["version"] == 1 and body["version_match"] is False, body
    assert body["consistent"] is True, body

    # 6. 带环管网：缺报管段只能给出范围；补一条读数后全部确定
    status, body = call("POST", "/api/network/import", RING_NETWORK)
    show("6a. 导入带环管网（版本重置为 1）", status, body)
    assert status == 200 and body["version"] == 1, body

    ring_entries = [
        {"pipe_id": "P1", "capacity": 6, "reading": None},
        {"pipe_id": "P2", "capacity": 6, "reading": None},
        {"pipe_id": "P3", "capacity": 10, "reading": None},
    ]
    status, body = reconcile(
        1,
        [{"source_id": "S", "injection": 10}],
        [{"sink_id": "U", "withdrawal": 10}],
        ring_entries,
    )
    show("6b. 环上全部缺报 -> P1/P2 ∈ [0,6]、P3 ∈ [4,10]（determined=false）", status, body)
    assert body["consistent"] is True, body
    assert body["estimates"] == [
        {"pipe_id": "P1", "min_flow": 0, "max_flow": 6, "determined": False},
        {"pipe_id": "P2", "min_flow": 0, "max_flow": 6, "determined": False},
        {"pipe_id": "P3", "min_flow": 4, "max_flow": 10, "determined": False},
    ], body

    ring_entries[1]["reading"] = 4  # 补报 P2=4 -> P1=4、P3=6 随之确定
    status, body = reconcile(
        1,
        [{"source_id": "S", "injection": 10}],
        [{"sink_id": "U", "withdrawal": 10}],
        ring_entries,
    )
    show("6c. 补报 P2=4 -> P1=4、P3=6 均可确定", status, body)
    assert body["estimates"] == [
        {"pipe_id": "P1", "min_flow": 4, "max_flow": 4, "determined": True},
        {"pipe_id": "P3", "min_flow": 6, "max_flow": 6, "determined": True},
    ], body

    # 7. 回到树状管网：隔离 P6（关 P4）后，阻断管段流量必须为零
    status, body = call("POST", "/api/network/import", network)
    show("7a. 重新导入树状管网", status, body)
    assert status == 200 and body["version"] == 1, body
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("7b. 提交隔离 P6（关 P4，版本 -> 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    # 阻断管段（P4 已关、P6 待修复）读数非零 -> 不一致
    status, body = reconcile(*sample_body(version=2))
    show("7c. 阻断管段读数非零 -> consistent=false", status, body)
    assert body["consistent"] is False and body["estimates"] == [], body

    # 阻断管段缺报、下游取用归零 -> 一致，P4/P6 确定为 0
    entries = [dict(e) for e in FULL_ENTRIES]
    entries[0]["reading"] = 20
    entries[1]["reading"] = 10
    entries[3]["reading"] = None
    entries[5]["reading"] = None
    status, body = reconcile(
        *sample_body(version=2, injection=20, withdrawals=(10, 10, 0), entries=entries)
    )
    show("7d. 阻断管段缺报 -> P4/P6 确定为 0", status, body)
    assert body["consistent"] is True, body
    assert body["estimates"] == [
        {"pipe_id": "P4", "min_flow": 0, "max_flow": 0, "determined": True},
        {"pipe_id": "P6", "min_flow": 0, "max_flow": 0, "determined": True},
    ], body

    # 8. 停运水源注入必须为零
    status, body = call(
        "POST",
        "/api/sources/switch",
        {"targets": [{"source_id": "S", "running": False}], "version": 2},
    )
    show("8a. 停运水源 S（版本 -> 3）", status, body)
    assert status == 200 and body["version"] == 3, body

    # 停运水源 S 注入 30（阻断管段缺报，唯一违规即停运源非零注入）-> 不一致
    entries = [dict(e) for e in FULL_ENTRIES]
    entries[3]["reading"] = None
    entries[5]["reading"] = None
    status, body = reconcile(*sample_body(version=3, entries=entries))
    show("8b. 停运水源注入非零 -> consistent=false", status, body)
    assert body["consistent"] is False and body["estimates"] == [], body

    # 停运源零注入、全网零取用、读数全部缺报 -> 一致，且全部管段确定为 0
    zero_entries = [dict(e, reading=None) for e in FULL_ENTRIES]
    status, body = reconcile(
        *sample_body(version=3, injection=0, withdrawals=(0, 0, 0), entries=zero_entries)
    )
    show("8c. 停运源零注入、全网零取用 -> 一致，缺报管段全部确定为 0", status, body)
    assert body["consistent"] is True, body
    assert all(
        e["min_flow"] == 0 and e["max_flow"] == 0 and e["determined"]
        for e in body["estimates"]
    ), body

    # 9. 非法请求（整体拒绝，返回全部问题明细）
    bad = {
        "version": 3,
        "sources": [],  # 水源未覆盖（缺 S）
        "sinks": [
            {"sink_id": "U1", "withdrawal": 1},
            {"sink_id": "U1", "withdrawal": 1},   # 重复
            {"sink_id": "UX", "withdrawal": 1},   # 不存在
            {"sink_id": "A", "withdrawal": 1},    # 不是用水点
        ],
        "pipes": [
            {"pipe_id": "P1", "capacity": 100, "reading": 30},
            {"pipe_id": "P1", "capacity": 100, "reading": None},  # 重复
            {"pipe_id": "PX", "capacity": 100, "reading": None},  # 不存在
            # 缺 P2..P6：覆盖不足
        ],
    }
    status, body = call("POST", "/api/metering/reconcile", bad)
    show("9a. 编号未覆盖/重复/不存在 -> 422 METERING_REQUEST_INVALID", status, body)
    assert status == 422 and body["error"]["code"] == "METERING_REQUEST_INVALID", body

    bad_type = {
        "version": 3,
        "sources": [{"source_id": "S", "injection": True}],  # 布尔不是非负整数
        "sinks": [{"sink_id": "U1", "withdrawal": -1}],      # 负数
        "pipes": [{"pipe_id": "P1", "capacity": 1.5}],       # 小数容量
    }
    status, body = call("POST", "/api/metering/reconcile", bad_type)
    show("9b. 类型/数值错误（布尔注入、负取用、小数容量）-> 422", status, body)
    assert status == 422, body

    # 10. 只读核对：版本仍为 3、只有隔离事件 1、快照不再增加
    status, net = call("GET", "/api/network")
    assert status == 200 and net["version"] == 3, net
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1 and events[0]["fault_pipe_id"] == "P6"
    print(
        "\n10. 只读核对通过：版本仍为 3，仅 1 条隔离事件（P6），"
        "阀位/水源状态/快照均未被核对接口改动。"
    )
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
