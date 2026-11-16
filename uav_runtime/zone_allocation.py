"""Translate solver region ownership into ordered rectangular work.

No solver, aircraft selection, flight submission, or inferred allocation here.
The future solver adapter must normalize its output to the documented contract.
"""
import copy
from .area_route import LocalFrame
import math

from .mission_models import digest, identifier, require


def positive(value, name):
    require(type(value) in (int, float) and math.isfinite(value) and value > 0,
            'INVALID_INPUT', name + ' must be finite and positive')
    return value


def factor_grid(count):
    require(type(count) is int and 1 <= count <= 64,
            'INVALID_INPUT', 'zone count must be an integer in 1..64')
    short = math.isqrt(count)
    while count % short:
        short -= 1
    return count // short, short


def partition_rectangle(region, count, scene=None):
    """Length runs west to east, width south to north; row-major SW first."""
    require(region.get('shape') == 'rectangle', 'INVALID_INPUT',
            'zone allocation currently requires an axis-aligned rectangle')
    length = positive(region['length_m'], 'length_m')
    width = positive(region['width_m'], 'width_m')
    lon, lat = region['center_longitude'], region['center_latitude']
    require(all(type(v) in (int, float) and math.isfinite(v) for v in (lon, lat))
            and -180 <= lon <= 180 and -90 < lat < 90,
            'INVALID_INPUT', 'invalid rectangle center')
    require(region.get('heading_deg', 0) == 0, 'INVALID_INPUT',
            'rotated rectangles are not supported')
    columns, rows = factor_grid(count)
    scene = LocalFrame(lon, lat) if scene is None else scene
    x, y = scene.to_xy(lon, lat)
    x0, y0 = x-length/2, y-width/2
    cells = []
    for row in range(rows):
        for col in range(columns):
            # Compute both ends from the same grid, sharing identical boundaries.
            west, east = x0+length*col/columns, x0+length*(col+1)/columns
            south, north = y0+width*row/rows, y0+width*(row+1)/rows
            center = scene.to_gps((west+east)/2, (south+north)/2)
            low, high = scene.to_gps(west,south), scene.to_gps(east,north)
            cells.append(dict(zone_index=len(cells)+1, row=row, column=col,
                shape='rectangle', center_longitude=center[0], center_latitude=center[1],
                length_m=length/columns, width_m=width/rows,
                area_m2=length*width/count, bounds_xy=[west,south,east,north],
                bounds_wgs84=[low[0],low[1],high[0],high[1]]))
    return dict(columns=columns, rows=rows, zones=cells)


def extract_zone_alloc(result):
    """Keep native counts, or derive counts from real region owners verbatim.

    This is the only temporary compatibility boundary. No task requirement,
    fleet size, or fabricated owner is used to create a count.
    """
    require(isinstance(result,dict) and result.get('status') in
            ('optimal','feasible','maxTimeLimit'),
            'INVALID_INPUT', 'allocation needs a successful solver result')
    regions = result.get('regions')
    require(isinstance(regions,list) and regions, 'INVALID_INPUT',
            'solver result needs region ownership')
    derived = {}
    for region in regions:
        require(isinstance(region,dict) and type(region.get('id')) is int
                and region['id'] > 0 and str(region['id']) not in derived,
                'INVALID_INPUT', 'invalid or duplicate solver region')
        owners = region.get('uavs')
        require(isinstance(owners,list) and owners
                and all(type(i) is int and i > 0 for i in owners)
                and len(set(owners)) == len(owners),
                'INVALID_INPUT', 'region owners must be distinct positive UAV IDs')
        derived[str(region['id'])] = len(owners)
    native = result.get('zone_alloc')
    if 'zone_alloc' in result:
        require(isinstance(native,dict) and set(native) == set(derived)
                and all(type(v) is int and v == derived[k] for k,v in native.items()),
                'INVALID_INPUT', 'native zone_alloc disagrees with solver region ownership')
        return copy.deepcopy(result), 'native_zone_alloc'
    normalized = copy.deepcopy(result)
    normalized['zone_alloc'] = derived
    return normalized, 'derived_from_regions_uavs'


def apply_allocation(request, scene, fleet):
    """Apply region counts and solver UAV order without changing either."""
    identifier(request['request_id'])
    uids = request['uav_ids']
    require(isinstance(uids,list) and 1 <= len(uids) <= 64
            and all(isinstance(u,str) for u in uids) and len(set(uids)) == len(uids)
            and set(uids) <= set(fleet), 'INVALID_INPUT', 'invalid candidate UAV mapping')
    for uid in uids:
        identifier(uid)
    tasks = request['tasks']
    require(isinstance(tasks,list) and 1 <= len(tasks) <= 64,
            'INVALID_INPUT', 'tasks must be a nonempty list of at most 64 tasks')
    ids = [t['region_id'] for t in tasks]
    tids = [identifier(t['task_id']) for t in tasks]
    require(all(type(i) is int and i > 0 for i in ids)
            and len(set(ids)) == len(ids) and len(set(tids)) == len(tids),
            'INVALID_INPUT', 'task and region IDs must be unique')
    result, count_source = extract_zone_alloc(request['result'])
    require(result.get('status') in ('optimal','feasible','maxTimeLimit'),
            'INVALID_INPUT', 'allocation needs a successful solver result')
    counts = result.get('zone_alloc')
    require(isinstance(counts,dict) and set(counts) == {str(i) for i in ids},
            'INVALID_INPUT', 'zone_alloc must map every region ID to its count')
    regions = result.get('regions')
    require(isinstance(regions,list) and len(regions) == len(tasks)
            and all(type(r.get('id')) is int for r in regions)
            and {r['id'] for r in regions} == set(ids),
            'INVALID_INPUT', 'result regions must match task region IDs exactly')
    by_region = {r['id']:r for r in regions}
    output = []; per_uav = {uid:[] for uid in uids}
    for task in tasks:
        rid = task['region_id']; count = counts[str(rid)]
        factor_grid(count)
        required = task['required_uavs']
        require(type(required) is int and required == count, 'INVALID_INPUT',
                'zone count must equal the task requested UAV count')
        owners = by_region[rid].get('uavs')
        require(isinstance(owners,list) and len(owners) == count
                and all(type(i) is int and 1 <= i <= len(uids) for i in owners)
                and len(set(owners)) == len(owners), 'INVALID_INPUT',
                'each zone needs a distinct UAV in solver output order')
        eligible = task.get('eligible_uav_ids',uids)
        require(isinstance(eligible,list) and all(isinstance(u,str) for u in eligible)
                and set(eligible) <= set(uids)
                and all(uids[i-1] in eligible for i in owners),
                'INVALID_INPUT', 'allocation violates task UAV eligibility')
        grid = partition_rectangle(task['region'],count)
        for zone, owner in zip(grid['zones'],owners):
            uid = uids[owner-1]
            work = copy.deepcopy(task)
            # Keep targets, tracking rules, patrol laps and completion conditions.
            work.update(uav_id=uid, zone_index=zone['zone_index'], region=zone)
            per_uav[uid].append(work)
        output.append(dict(task_id=task['task_id'],region_id=rid,
            columns=grid['columns'],rows=grid['rows'],
            assignments=[dict(uav_id=uids[i-1],zone=z) for z,i in zip(grid['zones'],owners)]))
    routes = result.get('uavs')
    if routes is not None:
        require(isinstance(routes,list) and len(routes) == len(uids)
                and all(type(r.get('id')) is int for r in routes)
                and {r['id'] for r in routes} == set(range(1,len(uids)+1)),
                'INVALID_INPUT', 'invalid UAV route mapping')
        for row in routes:
            uid = uids[row['id']-1]; nodes = row['route_nodes']
            require(isinstance(nodes,list) and nodes and nodes[0] == 0
                    and (len(nodes) == 1 or nodes[-1] == 0)
                    and all(type(n) is int and n in ids for n in nodes[1:-1]),
                    'INVALID_INPUT', 'invalid task route')
            work = {t['region_id']:t for t in per_uav[uid]}
            order = nodes[1:-1]
            require(len(order) == len(work) and set(order) == set(work),
                    'INVALID_INPUT', 'UAV route and zone ownership disagree')
            per_uav[uid] = [work[n] for n in order]
    else:
        require(all(len(work) <= 1 for work in per_uav.values()),
                'INVALID_INPUT', 'multi-task UAVs need solver route_nodes ordering')
    return dict(request_id=request['request_id'],submitted=False,stage='PARTITIONED',
        zone_alloc=result['zone_alloc'], zone_alloc_source=count_source,
        requires_trajectory_and_budget_validation=True, allocations=output,
        per_uav=per_uav,source=dict(input_digest=digest(request), map_used=False))
