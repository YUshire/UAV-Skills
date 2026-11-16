# -*- coding: utf-8 -*-
"""无人机任务分配 MILP 求解脚本（Pyomo + GLPK）。

符号与约束见 场景建模.md，输入输出字段见 接口文档.md。
运行： call setpyenv.bat && python solve_scenario.py <场景参数文件>
"""
import json
import os
import sys

from pyomo.environ import (
    Binary,
    ConcreteModel,
    Constraint,
    Expression,
    NonNegativeReals,
    Objective,
    Param,
    Set,
    SolverFactory,
    Var,
    minimize,
    value,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = "scenario_3uav_5region.json"
TMLIM = 120  # 求解时间上限，单位 s；求解器还包括 mipgap=0.001 的相对 gap 停止条件


def load(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def build(data):
    M, N = int(data["M"]), int(data["N"])
    I = list(range(1, M + 1))
    J = list(range(1, N + 1))
    K = list(range(0, N + 1))
    A = [(j, k) for j in K for k in K if j != k]
    P = [(i, ip) for i in I for ip in I if i < ip]
    uav = {u["id"]: u for u in data["uavs"]}

    eps = float(data["epsilon"])
    B = float(data["big_m"])
    lam = data["lambda"]
    # 出动架次权重，如未提供则默认为 0（不优化出动数量）
    lam_uav = lam.get("uav_count", 0.0)
    # 出动数量约束，K 给定时强制出动 K 架，缺省时不施加额外限制
    required_drones = data.get("required_drones", None)

    m = ConcreteModel("uav_patrol")
    m.I = Set(initialize=I, ordered=True)
    m.J = Set(initialize=J, ordered=True)
    m.A = Set(initialize=A, dimen=2)
    m.P = Set(initialize=P, dimen=2)

    # 参数
    m.s1 = Param(m.I, m.A, initialize={(i, j, k): uav[i]["transfer_range"][j][k] for i in I for j, k in A})
    m.t1 = Param(m.I, m.A, initialize={(i, j, k): uav[i]["transfer_time"][j][k] for i in I for j, k in A})
    m.s2b = Param(m.I, m.J, initialize={(i, j): uav[i]["patrol_range_base"][j] for i in I for j in J})
    m.t2b = Param(m.I, m.J, initialize={(i, j): uav[i]["patrol_time_base"][j] for i in I for j in J})
    m.Tmax = Param(m.I, initialize={i: uav[i]["max_endurance"] for i in I})
    Mt = {i: float(uav[i]["max_endurance"]) for i in I}
    Mt1 = max(uav[i]["transfer_time"][j][k] for i in I for j, k in A)

    # 决策变量
    tmax = max(uav[i]["max_endurance"] for i in I)
    m.x = Var(m.I, m.A, domain=Binary)
    m.u = Var(m.P, m.J, domain=Binary)
    m.delta = Var(m.I, domain=Binary)  # 无人机 i 是否被选用
    m.a = Var(m.I, m.J, domain=NonNegativeReals, bounds=lambda m, i, j: (0.0, m.Tmax[i]))
    m.w = Var(m.I, m.J, domain=NonNegativeReals, bounds=lambda m, i, j: (0.0, m.Tmax[i]))
    m.e = Var(m.I, domain=NonNegativeReals, bounds=(0.0, tmax))
    m.Cmax = Var(domain=NonNegativeReals, bounds=(0.0, tmax))

    # 派生量
    m.y = Expression(m.I, m.J, rule=lambda m, i, j: sum(m.x[i, j, k] for k in K if k != j))
    m.q = Expression(m.I, m.J, rule=lambda m, i, j: sum(m.y[ip, j] for ip in I if ip != i))
    m.t2 = Expression(m.I, m.J, rule=lambda m, i, j: m.t2b[i, j] * (1 - 0.5 * m.q[i, j]))
    m.s2 = Expression(m.I, m.J, rule=lambda m, i, j: m.s2b[i, j] * (1 - 0.5 * m.q[i, j]))
    m.b = Expression(m.I, m.J, rule=lambda m, i, j: m.a[i, j] + m.t2[i, j])
    m.z = Expression(m.I, m.J, rule=lambda m, i, j: sum(m.u[min(i, ip), max(i, ip), j] for ip in I if ip != i))
    m.L = Expression(
        m.I,
        rule=lambda m, i: sum(m.x[i, j, k] * m.s1[i, j, k] for j, k in A)
        + sum(m.y[i, j] * m.s2b[i, j] - 0.5 * m.s2b[i, j] * m.z[i, j] for j in J),
    )

    # 1 同机同区域至多巡逻一次（返航段也计入）
    m.con_once = Constraint(m.I, m.J, rule=lambda m, i, j: sum(m.x[i, j, k] for k in K if k != j) <= 1)

    # 2 同一区域最多两架无人机
    m.con_two = Constraint(m.J, rule=lambda m, j: sum(m.y[i, j] for i in I) <= 2)

    # 2b 每个区域至少被巡逻一次（场景建模.md 未列此约束，否则最优解为全部不动）
    m.con_cover = Constraint(m.J, rule=lambda m, j: sum(m.y[i, j] for i in I) >= 1)

    # 2c 无人机选用与任务执行关联：y_{i,j} <= delta_i
    m.con_delta = Constraint(m.I, m.J, rule=lambda m, i, j: m.y[i, j] <= m.delta[i])

    # 3 两机同区域巡逻区间必须重叠
    m.con_ov_a = Constraint(
        m.P, m.J,
        rule=lambda m, i, ip, j: m.a[i, j] + eps
        <= m.b[ip, j] + (Mt[i] + eps) * (2 - m.y[i, j] - m.y[ip, j]),
    )
    m.con_ov_b = Constraint(
        m.P, m.J,
        rule=lambda m, i, ip, j: m.a[ip, j] + eps
        <= m.b[i, j] + (Mt[ip] + eps) * (2 - m.y[i, j] - m.y[ip, j]),
    )

    # 4 u = y_i * y_ip 的线性化
    m.con_u1 = Constraint(m.P, m.J, rule=lambda m, i, ip, j: m.u[i, ip, j] <= m.y[i, j])
    m.con_u2 = Constraint(m.P, m.J, rule=lambda m, i, ip, j: m.u[i, ip, j] <= m.y[ip, j])
    m.con_u3 = Constraint(m.P, m.J, rule=lambda m, i, ip, j: m.u[i, ip, j] >= m.y[i, j] + m.y[ip, j] - 1)

    # 5 时序链：巡逻 j 结束并转场到 k 后才能开始 k 的巡逻
    m.con_chain = Constraint(
        m.I, m.J, m.J,
        rule=lambda m, i, j, k: Constraint.Skip if j == k
        else m.a[i, k] >= m.b[i, j] + m.t1[i, j, k] - (Mt[i] + Mt1) * (1 - m.x[i, j, k]),
    )

    # 6 同时出发：首段自集结点直飞，出发时刻为 0
    m.con_dep = Constraint(
        m.I, m.J,
        rule=lambda m, i, k: m.a[i, k] >= m.t1[i, 0, k] - Mt1 * (1 - m.x[i, 0, k]),
    )

    # 6b 有效不等式：任一区域的巡逻开始时刻不早于自集结点直飞该区域的时刻
    m.con_direct = Constraint(
        m.I, m.J,
        rule=lambda m, i, j: m.a[i, j] >= m.t1[i, 0, j] - Mt[i] * (1 - m.y[i, j]),
    )

    # 7 待机：w 为巡逻开始时刻与实际到达时刻之差
    m.con_w1 = Constraint(
        m.I, m.J,
        rule=lambda m, i, j: m.w[i, j] >= m.a[i, j] - m.t1[i, 0, j] - Mt[i] * (1 - m.x[i, 0, j]),
    )
    m.con_w2 = Constraint(
        m.I, m.J, m.J,
        rule=lambda m, i, h, j: Constraint.Skip if h == j
        else m.w[i, j] >= m.a[i, j] - m.b[i, h] - m.t1[i, h, j] - Mt[i] * (1 - m.x[i, h, j]),
    )

    # 8 续航（含返航段与待机）
    m.con_endur = Constraint(
        m.I,
        rule=lambda m, i: sum(m.x[i, j, k] * m.t1[i, j, k] for j, k in A)
        + sum(m.w[i, j] for j in J)
        + sum(m.t2b[i, j] * m.y[i, j] - 0.5 * m.t2b[i, j] * m.z[i, j] for j in J)
        <= m.Tmax[i],
    )

    # 9 出发段与返航段：至多一条，且巡逻必须有出发与返航
    m.con_dep_once = Constraint(m.I, rule=lambda m, i: sum(m.x[i, 0, k] for k in J) <= 1)
    m.con_ret_once = Constraint(m.I, rule=lambda m, i: sum(m.x[i, j, 0] for j in J) <= 1)
    m.con_dep_link = Constraint(m.I, m.J, rule=lambda m, i, j: sum(m.x[i, 0, k] for k in J) >= m.y[i, j])
    m.con_ret_link = Constraint(m.I, m.J, rule=lambda m, i, j: sum(m.x[i, h, 0] for h in J) >= m.y[i, j])

    # 9b 路径连通性：单商品流，保证被巡逻的区域都能自集结点依次到达
    m.f = Var(m.I, m.A, domain=NonNegativeReals, bounds=(0.0, float(N)))
    m.con_flow_cap = Constraint(m.I, m.A, rule=lambda m, i, j, k: m.f[i, j, k] <= N * m.x[i, j, k])
    m.con_flow_dep = Constraint(m.I, rule=lambda m, i: sum(m.f[i, 0, k] for k in J) == sum(m.y[i, j] for j in J))
    m.con_flow_node = Constraint(
        m.I, m.J,
        rule=lambda m, i, j: sum(m.f[i, h, j] for h in K if h != j)
        - sum(m.f[i, j, k] for k in K if k != j)
        == m.y[i, j],
    )

    # 9c 出动数量约束：K 给定时精确等于 K，缺省时上界为 M（自动满足）
    if required_drones is not None:
        req_K = int(required_drones)
        if not (1 <= req_K <= M):
            raise ValueError("required_drones 必须在 1 到 %d 之间，当前为 %d" % (M, req_K))
        m.con_uav_count = Constraint(rule=lambda m: sum(m.delta[i] for i in I) == req_K)
        print("已启用出动数量等式约束：sum(delta_i) = %d" % req_K)
    else:
        # K 缺省时不施加额外约束（上界为 M 自动满足）
        pass

    # 10 终末时刻与完工时间
    m.con_e = Constraint(
        m.I, m.J,
        rule=lambda m, i, j: m.e[i] >= m.b[i, j] + m.t1[i, j, 0] - (Mt[i] + Mt1) * (1 - m.x[i, j, 0]),
    )
    m.con_cmax = Constraint(m.I, rule=lambda m, i: m.Cmax >= m.e[i])

    # 10b 有效不等式：单机终末时刻不低于其航线上各段最短耗时之和
    t2half = {(i, j): (uav[i]["patrol_time_base"][j] / 2.0 if j >= 1 else 0.0) for i in I for j in K}
    m.con_e_lb = Constraint(
        m.I,
        rule=lambda m, i: m.e[i] >= sum(m.x[i, j, k] * (m.t1[i, j, k] + t2half[i, j]) for j, k in A),
    )

    # 目标：四项按基准值归一化后加权
    # 注：K 给定时 delta_i 项为常数，lambda_4 建议置 0
    ref_cmax = B
    ref_range = sum(uav[i]["transfer_range"][j][k] for i in I for j, k in A)
    if ref_range <= 0:
        # Co-located jobs may have zero transfers but positive work. Keep the
        # historical scale for every positive-transfer scenario.
        ref_range = sum(uav[i]["patrol_range_base"][j] for i in I for j in J)
        if ref_range <= 0:
            ref_range = 1.0
    ref_wait = B
    ref_uav = float(M)  # 出动架次归一化基准：可用总架数

    # K 给定时 lambda_uav 项为常数，建议忽略
    if required_drones is not None and lam_uav != 0.0:
        print("警告：required_drones 已指定为 %d，目标函数中 lambda_uav 项为常数，建议置 0" % required_drones)

    m.obj = Objective(
        expr=lam["makespan"] * m.Cmax / ref_cmax
        + lam["total_range"] * sum(m.L[i] for i in I) / ref_range
        + lam["total_wait"] * sum(m.w[i, j] for i in I for j in J) / ref_wait
        + lam_uav * sum(m.delta[i] for i in I) / ref_uav,
        sense=minimize,
    )
    return m, (I, J, K, A, ref_cmax, ref_range, ref_wait, ref_uav)


def rnd(x, nd=3):
    return round(float(x), nd)


def collect(m, idx):
    """把解整理成接口输出的数据结构。"""
    I, J, K, A, ref_cmax, ref_range, ref_wait, ref_uav = idx
    total_range = sum(value(m.L[i]) for i in I)
    total_wait = sum(value(m.w[i, j]) for i in I for j in J)
    uav_count = int(sum(value(m.delta[i]) for i in I))
    out = {
        "objective": rnd(value(m.obj), 6),
        "objective_terms": {
            "makespan": rnd(value(m.Cmax)),
            "total_range": rnd(total_range),
            "total_wait": rnd(total_wait),
            "uav_count": uav_count,
        },
        "normalization": {
            "makespan": ref_cmax,
            "total_range": ref_range,
            "total_wait": ref_wait,
            "uav_count": ref_uav,
        },
        "makespan": rnd(value(m.Cmax)),
        "total_range": rnd(total_range),
        "total_wait": rnd(total_wait),
        "uav_count": uav_count,
        # 各区域执行巡逻的无人机数量；0 号为起终点，不统计
        "zone_alloc": {str(j): sum(1 for i in I if value(m.y[i, j]) > 0.5) for j in J},
        "uavs": [],
        "regions": [],
    }
    for i in I:
        arcs = [(j, k) for (j, k) in A if value(m.x[i, j, k]) > 0.5]
        arcs.sort(key=lambda jk: -1.0 if jk[0] == 0 else value(m.a[i, jk[0]]))
        segs = []
        nodes = [0]
        for n, (j, k) in enumerate(arcs, start=1):
            patrolling = j >= 1
            segs.append({
                "index": n,
                "from": j,
                "to": k,
                "patrol_region": j if patrolling else None,
                "start_time": rnd(value(m.a[i, j])) if patrolling else None,
                "end_time": rnd(value(m.b[i, j])) if patrolling else None,
                "wait_time": rnd(value(m.w[i, j])) if patrolling else 0.0,
                "patrol_time": rnd(value(m.t2[i, j])) if patrolling else None,
                "patrol_range": rnd(value(m.s2[i, j])) if patrolling else None,
                "transfer_time": rnd(value(m.t1[i, j, k])),
                "transfer_range": rnd(value(m.s1[i, j, k])),
            })
            nodes.append(k)
        used = (
            sum(value(m.x[i, j, k]) * value(m.t1[i, j, k]) for j, k in A)
            + sum(value(m.w[i, j]) for j in J)
            + sum(value(m.t2[i, j]) * value(m.y[i, j]) for j in J)
        )
        out["uavs"].append({
            "id": i,
            "departure_node": next((s["to"] for s in segs if s["from"] == 0), None),
            "return_node": next((s["from"] for s in segs if s["to"] == 0), None),
            "route_nodes": nodes,
            "segments": segs,
            "total_range": rnd(value(m.L[i])),
            "finish_time": rnd(used),
            "endurance_used": rnd(used),
            "endurance_limit": value(m.Tmax[i]),
        })
    for j in J:
        who = [i for i in I if value(m.y[i, j]) > 0.5]
        row = {
            "id": j,
            "uavs": who,
            "patrol_times": [rnd(value(m.t2[i, j])) for i in who],
            "overlapping": len(who) > 1,
            "overlap_interval": None,
        }
        if len(who) > 1:
            a, b = who[0], who[1]
            row["overlap_interval"] = [
                rnd(max(value(m.a[a, j]), value(m.a[b, j]))),
                rnd(min(value(m.b[a, j]), value(m.b[b, j]))),
            ]
        out["regions"].append(row)
    return out


def print_report(out):
    print("目标值 J = %.6f" % out["objective"])
    print("完工时间 Cmax = %.1f s" % out["makespan"])
    print("实际出动无人机数量 = %d 架" % out["uav_count"])
    print()
    for u in out["uavs"]:
        parts = ["0"]
        for seg in u["segments"]:
            if seg["patrol_region"] is None:
                parts.append(" -[转场%.0fs]-> %d" % (seg["transfer_time"], seg["to"]))
            else:
                parts.append(
                    " -[巡逻%d %.0fs,待机%.0fs]-> %d"
                    % (seg["patrol_region"], seg["patrol_time"], seg["wait_time"], seg["to"])
                )
        print("无人机 %d：%s" % (u["id"], "".join(parts)))
        print(
            "  里程 L=%8.1f m  终末 e=%7.1f s  续航占用=%7.1f/%7.1f s"
            % (u["total_range"], u["finish_time"], u["endurance_used"], u["endurance_limit"])
        )
    print()
    print("总里程 = %.1f m，总待机 = %.1f s" % (out["total_range"], out["total_wait"]))
    print()
    for r in out["regions"]:
        if not r["uavs"]:
            print("区域 %d：无无人机巡逻" % r["id"])
        elif len(r["uavs"]) == 1:
            print("区域 %d：无人机 %d 单独巡逻，巡逻时间 %.1f s" % (r["id"], r["uavs"][0], r["patrol_times"][0]))
        else:
            print(
                "区域 %d：无人机 %d 与 %d 同时巡逻，巡逻时间 %.1f / %.1f s（基准值一半），重叠 [%.1f, %.1f]"
                % (r["id"], r["uavs"][0], r["uavs"][1], r["patrol_times"][0], r["patrol_times"][1],
                   r["overlap_interval"][0], r["overlap_interval"][1])
            )
    print()
    print("出发段与返航段：")
    for u in out["uavs"]:
        print("  无人机 %d：出发 0->%s，返航 %s->0"
              % (u["id"], u["departure_node"] if u["departure_node"] is not None else "无",
                 u["return_node"] if u["return_node"] is not None else "无"))


def dump(out, status, scenario, solver_name, solver_dur, path):
    payload = {
        "status": status,
        "scenario": scenario,
        "solver": solver_name,
        "solver_dur": solver_dur,
    }
    payload.update(out)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def main():
    import time
    t0 = time.time()

    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="无人机任务分配 MILP 求解器（Pyomo + GLPK）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python solve_scenario.py --input %s" % SAMPLE,
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        metavar="INPUT",
        help="场景参数 JSON 文件路径（必填）",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        metavar="OUTPUT",
        help="结果输出 JSON 文件路径（可选；默认当前目录，文件名规则：_pro.json → _sol.json，.json → _sol.json）",
    )
    args = parser.parse_args()

    data_path = os.path.abspath(args.input)
    if not os.path.isfile(data_path):
        parser.error("场景参数文件不存在：%s" % data_path)

    # 确定输出路径
    base = os.path.basename(data_path)
    if args.output:
        out_path = os.path.abspath(args.output)
    else:
        # _pro.json → _sol.json；.json → _sol.json
        if base.endswith("_pro.json"):
            out_name = base[:-9] + "_sol.json"  # 去掉 _pro.json，加 _sol.json
        elif base.endswith(".json"):
            out_name = base[:-5] + "_sol.json"  # 去掉 .json，加 _sol.json
        else:
            out_name = base + "_sol.json"       # 无 .json 后缀，直接拼接
        out_path = os.path.abspath(out_name)    # 当前执行目录

    data = load(data_path)
    m, idx = build(data)
    solver = SolverFactory("glpk")
    if not solver.available(exception_flag=False):
        raise SystemExit("未找到 glpsol，请确认已 call setpyenv.bat 激活含 GLPK 的环境")
    solver.options["mipgap"] = 0.001
    solver.options["tmlim"] = TMLIM

    res = solver.solve(m, tee=False)
    solver_dur = time.time() - t0

    print("求解耗时：%.2f s" % solver_dur)
    from pyomo.environ import Var as _Var
    nbin = sum(1 for v in m.component_data_objects(_Var) if v.is_binary())
    print("变量数 %d（其中 0-1 变量 %d），约束数 %d" % (m.nvariables(), nbin, m.nconstraints()))
    print()

    out = collect(m, idx)

#   print_report(out)

    dump(out, str(res.solver.termination_condition), base, "glpk", round(solver_dur, 2), out_path)

    print()
    print("结果已写入 %s" % out_path)


if __name__ == "__main__":
    main()
