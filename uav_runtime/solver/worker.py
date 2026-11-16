#!/usr/bin/env python3
"""Private subprocess protocol: JSON stdin/stdout; optional solver dependencies."""
import contextlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    from uav_runtime.scheduler import validate_scenario
    from pyomo.environ import Binary, Constraint, ConstraintList, SolverFactory, Var, value

    request = json.load(sys.stdin)
    data = request["scenario"]
    validate_scenario(data)
    if data.get("required_drones") == -1:
        data = dict(data, required_drones=None)
    source = ROOT / "uav_runtime/solver/solve_scenario.py"
    spec = importlib.util.spec_from_file_location("upstream_scheduler", source)
    upstream = importlib.util.module_from_spec(spec)
    protocol_output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(upstream)
        model, index = upstream.build(data)
        model.project_limits = ConstraintList()
        if "job_groups" in data:
            job_groups = data["job_groups"]
            groups = sorted(set(job_groups.values()))
            model.group_choice = Var(model.I, groups, domain=Binary)
            for uid in model.I:
                model.project_limits.add(sum(model.group_choice[uid, gid] for gid in groups) == model.delta[uid])
                for job in model.J:
                    model.project_limits.add(model.y[uid, job] <= model.group_choice[uid, job_groups[str(job)]])
                    for other in model.J:
                        if job_groups[str(job)] != job_groups[str(other)]:
                            model.x[uid, job, other].fix(0)
        if data.get('new_reserve_indices'):
            model.project_limits.add(sum(model.delta[uid] for uid in data['new_reserve_indices']) <= data['max_new_reserves'])
        if "max_active_uavs" in data:
            model.project_limits.add(
                sum(model.delta[uid] for uid in model.I) <= data["max_active_uavs"]
            )
        # Upstream only links y <= delta. Count real departures, never idle
        # aircraft with delta=1 that were selected solely to satisfy K.
        for uid in model.I:
            model.project_limits.add(
                sum(model.x[uid, 0, job] for job in model.J) == model.delta[uid]
            )
            model.project_limits.add(
                sum(model.x[uid, job, 0] for job in model.J) == model.delta[uid]
            )
            for job in model.J:
                nodes = [0] + list(model.J)
                model.project_limits.add(
                    sum(model.x[uid, other, job] for other in nodes if other != job)
                    == sum(model.x[uid, job, other] for other in nodes if other != job)
                )
        for job in data.get("single_owner_jobs", []):
            model.project_limits.add(sum(model.y[uid, job] for uid in model.I) == 1)
        for job, uid in data.get("fixed_assignments", {}).items():
            for candidate in model.I:
                model.project_limits.add(
                    model.y[candidate, int(job)] == (1 if candidate == uid else 0)
                )
        for job, ids in data.get("eligible_uavs", {}).items():
            for uid in model.I:
                if uid not in ids:
                    model.project_limits.add(model.y[uid, int(job)] == 0)
        for uav in data["uavs"]:
            if "max_range" in uav:
                model.project_limits.add(model.L[uav["id"]] <= uav["max_range"])
        solver = SolverFactory("highs")
        solver.options["time_limit"] = request["time_limit"]
        solver.options["mip_rel_gap"] = 0.001
        # Inspect termination before loading: lack of an incumbent at a time
        # limit is not proof that this fleet cannot perform the mission.
        solved = solver.solve(model, load_solutions=False)
        termination = str(solved.solver.termination_condition)
        if not solved.solution:
            code = (
                "PLANNING_INFEASIBLE"
                if termination == "infeasible"
                else (
                    "SOLVER_TIMEOUT"
                    if termination == "maxTimeLimit"
                    else "SOLVER_FAILED"
                )
            )
            json.dump(
                dict(
                    error=dict(code=code, message="solver terminated: " + termination)
                ),
                protocol_output,
            )
            return 1
        model.solutions.load_from(solved)
        # Check the actual incumbent constraints before collect() rounds values.
        for constraint in model.component_data_objects(Constraint, active=True):
            body = value(constraint.body)
            if (constraint.has_lb() and body < value(constraint.lower) - 1e-5) or (
                constraint.has_ub() and body > value(constraint.upper) + 1e-5
            ):
                raise ValueError("solver incumbent violates model constraints")
        for variable in model.component_data_objects(Var):
            if (
                variable.is_binary()
                and abs(value(variable) - round(value(variable))) > 1e-5
            ):
                raise ValueError("nonintegral solver incumbent")
        output = upstream.collect(model, index)
        for row in output["uavs"]:
            row["selected"] = value(model.delta[row["id"]]) > 0.5
        # collect() uses int(sum(delta)), which truncates e.g. 2.999999999.
        # Binary integrality was checked above; count the selected flags.
        output["uav_count"] = sum(row["selected"] for row in output["uavs"])
        output["objective_terms"]["uav_count"] = output["uav_count"]
        output.update(status=str(solved.solver.termination_condition), solver="HiGHS")
    json.dump(output, sys.stdout, allow_nan=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        code = (
            "SOLVER_UNAVAILABLE"
            if isinstance(exc, ImportError)
            else (
                "NO_FEASIBLE_SCHEDULE"
                if type(exc).__name__ == "NoFeasibleSolutionError"
                else "SOLVER_FAILED"
            )
        )
        messages = {
            "SOLVER_UNAVAILABLE": "optional Pyomo/HiGHS environment is unavailable",
            "NO_FEASIBLE_SCHEDULE": "no feasible schedule was loaded within the solver budget",
            "SOLVER_FAILED": "scheduler could not produce a verified result",
        }
        json.dump(dict(error=dict(code=code, message=messages[code])), sys.stdout)
        raise SystemExit(1)
