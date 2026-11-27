"""Prepare existing fleet records and explicit geographic jobs for seven skills."""
from copy import deepcopy
import importlib.util
import math

from .atomic.common import AtomicError, require, required
from .budget_math import DEFAULT_REF_ALT_M
from .direct_path import EARTH_RADIUS_M, direct_route
from .paths import ROOT


def number(value, field, positive=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
        result = float(value) if valid else None
    except (OverflowError, ValueError):
        result = None
    if result is None or (positive and result <= 0):
        raise AtomicError('INVALID_ARGUMENT', field + ' 必须是有限数值' + ('且大于0' if positive else ''), field=field)
    return result


def coordinate(lon, lat, field):
    lon, lat = number(lon, field + '.longitude'), number(lat, field + '.latitude')
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise AtomicError('INVALID_ARGUMENT', field + ' 超出WGS84范围', field=field)
    return [lon, lat]


def prepare_candidates(data):
    """查询当前只读机队记录，按给定候选条件筛选并返回数值预算。"""
    from .atomic.query import compute as query
    from .atomic.selection import compute as select
    require(data, 'input', dict)
    spatial_fields = {'center_lon', 'center_lat', 'radius_m', 'job_radius_m'}
    if set(data) - ({'uav_ids', 'exclude_uavs', 'payload', 'flight_alt'} | spatial_fields):
        raise AtomicError('INVALID_ARGUMENT', '候选查询含不支持的字段')
    explicit_alt = data.get('flight_alt') is not None
    altitude = number(data['flight_alt'], 'flight_alt', True) if explicit_alt else DEFAULT_REF_ALT_M
    has_lon, has_lat = 'center_lon' in data, 'center_lat' in data
    if has_lon != has_lat:
        raise AtomicError('INVALID_ARGUMENT', 'center_lon与center_lat须成对提供')
    center = coordinate(data['center_lon'], data['center_lat'], 'center') if has_lon else None
    radius = number(data['radius_m'], 'radius_m', True) if data.get('radius_m') is not None else None
    job_radius = number(data.get('job_radius_m', 0), 'job_radius_m')
    if job_radius < 0:
        raise AtomicError('INVALID_ARGUMENT', 'job_radius_m 不能为负')
    if radius is not None and center is None:
        raise AtomicError('INVALID_ARGUMENT', 'radius_m 需要同时提供 center_lon/center_lat')
    spatial_query = center is not None and not data.get('uav_ids')
    max_range = None
    bbox = None
    if spatial_query:
        if radius is None:
            max_row = query({'entity': 'resources',
                             'sql': 'SELECT MAX(max_range) AS max_range FROM uav_resource'})
            max_range = number(max_row['records'][0]['max_range'], 'max_range', True)
            radius = max_range / 2 + job_radius
        # SQL-side bounding box: only in-box status rows are read; the exact
        # circle is applied below with great-circle distance.
        lat_delta = math.degrees(radius / EARTH_RADIUS_M)
        lon_delta = lat_delta / max(math.cos(math.radians(center[1])), 1e-9)
        bbox = {'min_lon': center[0] - lon_delta, 'max_lon': center[0] + lon_delta,
                'min_lat': center[1] - lat_delta, 'max_lat': center[1] + lat_delta}
    facts = query({'entity': 'fleet', 'bounds': bbox} if bbox else {'entity': 'fleet'})
    bbox_input_count = len(facts['fleet'])
    # Missing membership is a concrete exclusion, even if another table carries
    # similarly named fields. It cannot be repaired by joining unrelated values.
    fleet = []
    missing = []
    for record in facts['fleet']:
        if not all(record.get(key) is True for key in ('has_status', 'has_resource')):
            missing.append({'uav_id': record['uav_id'], 'reasons': ['状态库或资源库缺记录']})
        else:
            fleet.append(record)
    spatial_filter = None
    circle_excluded = 0
    if spatial_query:
        spatial_filter = {'center': center, 'radius_m': radius, 'bbox': bbox,
                          'bbox_input_count': bbox_input_count,
                          'circle_excluded_count': 0,
                          'max_range_m': max_range, 'job_radius_m': job_radius}
        if not facts['fleet']:
            raise AtomicError('NO_CANDIDATE', '附近预筛范围内没有机队记录',
                              spatial_filter=spatial_filter)
        near = []
        for record in fleet:
            origin = record.get('origin') or {}
            try:
                distance = direct_route(
                    (number(origin.get('longitude'), 'origin.longitude'),
                     number(origin.get('latitude'), 'origin.latitude')),
                    (center[0], center[1]), 1.0, points=0)['one_way_distance']
            except AtomicError:
                near.append(record)  # let selection report the invalid origin
                continue
            if distance <= radius + 1e-6:
                near.append(record)
        circle_excluded = len(fleet) - len(near)
        spatial_filter['circle_excluded_count'] = circle_excluded
        fleet = near
        if not fleet:
            raise AtomicError('NO_CANDIDATE', '附近预筛范围内没有机队记录',
                              spatial_filter=spatial_filter)
    selection_input = {key: value for key, value in data.items() if key not in spatial_fields}
    if not explicit_alt:
        selection_input.pop('flight_alt', None)
    selected = select(dict(selection_input, fleet=fleet, **({'flight_alt': altitude} if explicit_alt else {})))
    relevant_missing = [row for row in missing if not data.get('uav_ids') or row['uav_id'] in data['uav_ids']]
    excluded = selected['excluded'] + relevant_missing
    if data.get('uav_ids') and excluded:
        # Report other usable aircraft so a named-aircraft failure does not
        # read as "the whole fleet cannot do it".
        alternative_input = {key: value for key, value in selection_input.items() if key != 'uav_ids'}
        try:
            alternatives = select(dict(alternative_input, fleet=fleet, **({'flight_alt': altitude} if explicit_alt else {})))['candidates']
        except AtomicError:
            alternatives = []
        available = [{key: row.get(key) for key in
                      ('uav_id', 'range_budget', 'time_budget', 'max_altitude', 'max_speed',
                       'reserve_battery_percent')}
                     for row in alternatives]
        available.sort(key=lambda row: row.get('range_budget') or 0, reverse=True)
        altitudes = []
        for row in fleet:
            try:
                altitudes.append(number(row.get('max_altitude'), 'max_altitude', True))
            except AtomicError:
                continue
        max_recorded_altitude = max(altitudes) if altitudes else None
        message = '用户点名的候选机未全部通过筛选'
        message += (f'；另有 {len(available)} 架可用候选可作替代' if available
                    else '；当前筛选条件下没有替代候选')
        if max_recorded_altitude is not None and max_recorded_altitude < altitude:
            message += (f'；记录中的最高升限为 {max_recorded_altitude:g} m，'
                        f'低于请求的 {altitude:g} m')
        raise AtomicError('CANDIDATE_EXCLUDED', message, excluded=excluded,
                          available_count=len(available), available_candidates=available[:20],
                          max_recorded_altitude=max_recorded_altitude)
    candidates = deepcopy(selected['candidates'])
    for row in candidates:
        row['battery'] = row['battery_level']
    coordinate_filter = None
    if center is not None:
        # Reuse the final action converter; its projection and limit are the source of truth.
        spec = importlib.util.spec_from_file_location(
            '_candidate_coordinate_validator', ROOT / 'skills/flight-control/scripts/submit_queue_batch.py')
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        in_range, coordinate_excluded = [], []
        for row in candidates:
            origin = row['origin']
            try:
                local_origin = validator.validate_origin(origin['latitude'], origin['longitude'])
            except validator.ValidationError as exc:
                raise AtomicError('INVALID_ARGUMENT', str(exc), uav_id=row['uav_id']) from exc
            try:
                validator.wgs84_to_local(*center, altitude, local_origin,
                                         validator.DEFAULT_MAX_DISTANCE_M, 'job_center')
            except validator.ValidationError as exc:
                coordinate_excluded.append({
                    'uav_id': row['uav_id'], 'reasons': ['目标中心超出坐标转换范围'],
                    'constraint': 'COORDINATE_RANGE', 'message': str(exc),
                    'origin': deepcopy(origin), 'target_center': center,
                    'range_budget': row['range_budget'], 'time_budget': row['time_budget']})
            else:
                in_range.append(row)
        # shortcut: only the supplied centre is known here; final assembly still checks all points.
        coordinate_filter = {'scope': 'known_job_center_only',
                             'max_distance_m': validator.DEFAULT_MAX_DISTANCE_M,
                             'excluded_count': len(coordinate_excluded)}
        excluded.extend(coordinate_excluded)
        if coordinate_excluded and (data.get('uav_ids') or not in_range):
            raise AtomicError('CANDIDATE_EXCLUDED' if data.get('uav_ids') else 'COORDINATE_RANGE_EXCEEDED',
                              '候选机的目标中心超出当前坐标转换范围；这是工具限制，不代表物理航程不足',
                              coordinate_filter=coordinate_filter, excluded=excluded,
                              available_candidates=in_range, available_count=len(in_range))
        candidates = in_range
    candidates_top, reachable_lb_count = _candidate_briefs(candidates, center)
    return {'candidates': candidates, 'excluded': excluded, 'flight_alt': altitude,
            'reserve_battery_percent': selected['reserve_battery_percent'],
            'source': 'configured_readonly_snapshot', 'submitted': False,
            '_summary': {'candidate_count': len(candidates),
                         'excluded_count': len(excluded),
                         'reachable_lb_count': reachable_lb_count,
                         'candidates_top': candidates_top,
                         'bbox_input_count': bbox_input_count if spatial_filter else None,
                         'circle_excluded_count': circle_excluded,
                         'coordinate_filter': coordinate_filter,
                         'flight_alt': altitude,
                         'spatial_filter': spatial_filter}}


def _candidate_briefs(candidates, center, limit=15):
    """Build compact, reachability-ranked candidate summaries to avoid large artifact reads."""
    items = []
    for row in candidates:
        origin = row.get('origin') or {}
        rb = row.get('range_budget')
        tb = row.get('time_budget')
        mfr = row.get('max_flight_radius')
        brief = {
            'uav_id': row['uav_id'],
            'payload': row.get('payload_status'),
            'max_endurance_min': row.get('max_endurance'),
            'battery_pct': row.get('battery_level'),
            'max_flight_radius_m': mfr,
            'range_budget_m': round(rb, 1) if isinstance(rb, (int, float)) else rb,
            'time_budget_s': round(tb, 1) if isinstance(tb, (int, float)) else tb,
        }
        if center is not None and 'longitude' in origin and 'latitude' in origin:
            dist = direct_route((origin['longitude'], origin['latitude']),
                                (center[0], center[1]), 1.0, points=0)['one_way_distance']
            radius_ok = (mfr is None) or (dist <= mfr + 1e-6)
            range_ok = isinstance(rb, (int, float)) and (2.0 * dist <= rb + 1e-6)
            brief['dist_m'] = round(dist, 1)
            brief['radius_ok'] = radius_ok
            brief['range_ok'] = range_ok
            brief['reachable_lb'] = radius_ok and range_ok
            brief['origin'] = [round(origin['longitude'], 6), round(origin['latitude'], 6)]
        else:
            if 'longitude' in origin and 'latitude' in origin:
                brief['origin'] = [round(origin['longitude'], 6), round(origin['latitude'], 6)]
        items.append(brief)
    if center is not None:
        items.sort(key=lambda x: (not x.get('reachable_lb', False),
                                  x.get('dist_m', float('inf')),
                                  -(x.get('max_endurance_min') or 0)))
        reachable_lb_count = sum(1 for x in items if x.get('reachable_lb'))
    else:
        items.sort(key=lambda x: -(x.get('range_budget_m') or 0))
        reachable_lb_count = None
    return items[:limit], reachable_lb_count


def resolve_jobs(data):
    """把已查询地点记录与用户明确区域参数转为数值作业；不查询数据库。"""
    require(data, 'input', dict)
    if set(data) - {'jobs', 'places'}:
        raise AtomicError('INVALID_ARGUMENT', '区域整理含不支持的字段')
    raw_jobs = require(required(data, 'jobs'), 'jobs', list)
    if not 1 <= len(raw_jobs) <= 64:
        raise AtomicError('INVALID_ARGUMENT', 'jobs 数量须为1..64')
    places = require(data.get('places', []), 'places', list)
    indexed = {}
    for row in places:
        require(row, 'places[]', dict)
        name, alias = row.get('region_name'), row.get('alias')
        if not isinstance(name, str) or not name.strip() or (alias is not None and not isinstance(alias, str)):
            raise AtomicError('INVALID_ARGUMENT', '地点记录需要非空region_name及字符串或null的alias')
        for label in set([name] + [part.strip() for part in (alias or '').split(';') if part.strip()]):
            indexed.setdefault(label, []).append(row)
    allowed = {'job_id', 'name', 'place', 'center_lon', 'center_lat', 'shape', 'radius', 'length', 'width', 'uav_id',
               'task_type', 'path_mode', 'entry_side', 'direction', 'laps', 'search_timeout'}
    jobs, seen = [], set()
    for i, item in enumerate(raw_jobs, 1):
        require(item, 'jobs[]', dict)
        if set(item) - allowed:
            raise AtomicError('INVALID_ARGUMENT', '作业含不支持的字段', fields=sorted(set(item) - allowed))
        jid = item.get('job_id', 'J' + str(i))
        if not isinstance(jid, str) or not jid.strip() or jid in seen:
            raise AtomicError('INVALID_ARGUMENT', 'job_id须为唯一非空字符串')
        seen.add(jid)
        row = None
        place = item.get('place')
        if 'place' in item:
            if not isinstance(place, str) or not place.strip():
                raise AtomicError('INVALID_ARGUMENT', 'place须为非空字符串')
            matches = indexed.get(place, [])
            if not matches:
                raise AtomicError('NOT_FOUND', '提供的地点记录中没有该地名或别名', place=place)
            if len(matches) != 1:
                raise AtomicError('AMBIGUOUS', '地点匹配到多条记录', place=place)
            row = matches[0]
        name = item.get('name', place or jid)
        if not isinstance(name, str) or not name.strip():
            raise AtomicError('INVALID_ARGUMENT', 'name须为非空字符串')
        shape = item.get('shape') or (row or {}).get('region_shape')
        if shape not in ('rectangle', 'circle'):
            raise AtomicError('MISSING_PARAMETER' if not shape else 'INVALID_ARGUMENT',
                              '区域需要rectangle或circle及明确尺寸', job_id=jid)
        sizes = {}
        if shape == 'circle':
            if 'radius' not in item:
                raise AtomicError('MISSING_PARAMETER', '圆形区域需要用户给出radius', job_id=jid)
            sizes['radius'] = number(item['radius'], 'radius', True)
        else:
            for field, registry in [('length', 'region_length_m'), ('width', 'region_width_m')]:
                value = item[field] if field in item else (row or {}).get(registry)
                if value is None:
                    raise AtomicError('MISSING_PARAMETER', '矩形区域缺少' + field, job_id=jid)
                sizes[field] = number(value, field, True)
        if 'center_lon' in item or 'center_lat' in item:
            if 'center_lon' not in item or 'center_lat' not in item:
                raise AtomicError('MISSING_PARAMETER', 'center_lon和center_lat须成对提供', job_id=jid)
            center = coordinate(item['center_lon'], item['center_lat'], 'center')
        elif row is not None:
            lon, lat = row.get('region_center_longitude'), row.get('region_center_latitude')
            if lon is None and lat is None and not row.get('region_shape'):
                # An explicitly sized area around a registered point uses that
                # point; no default radius or inferred extent is introduced.
                center = coordinate(row.get('longitude'), row.get('latitude'), 'point')
            elif lon is None or lat is None:
                raise AtomicError('MISSING_PARAMETER', '登记区域中心不完整', job_id=jid)
            else:
                center = coordinate(lon, lat, 'region_center')
        else:
            raise AtomicError('MISSING_PARAMETER', '需要地点记录或成对中心坐标', job_id=jid)
        entrance = None
        if row is not None:
            lon, lat = row.get('longitude'), row.get('latitude')
            if lon is not None or lat is not None:
                if lon is None or lat is None:
                    raise AtomicError('MISSING_PARAMETER', '地点入口坐标不完整', job_id=jid)
                entrance = coordinate(lon, lat, 'entrance')
        fixed = item.get('uav_id')
        if fixed is not None and (not isinstance(fixed, str) or not fixed.strip()):
            raise AtomicError('INVALID_ARGUMENT', 'uav_id须为非空字符串')
        step = {}
        task_type = item.get('task_type')
        if task_type is not None:
            if task_type not in ('search', 'patrol'):
                raise AtomicError('INVALID_ARGUMENT', 'task_type 须为 search 或 patrol', job_id=jid)
            step['task_type'] = task_type
        path_mode = item.get('path_mode')
        if path_mode is not None:
            if path_mode not in ('coverage', 'boundary'):
                raise AtomicError('INVALID_ARGUMENT', 'path_mode 须为 coverage 或 boundary', job_id=jid)
            step['path_mode'] = path_mode
        entry_side = item.get('entry_side')
        if entry_side is not None:
            if entry_side not in ('south', 'north', 'east', 'west'):
                raise AtomicError('INVALID_ARGUMENT', 'entry_side 须为 south/north/east/west', job_id=jid)
            step['entry_side'] = entry_side
        direction = item.get('direction')
        if direction is not None:
            if direction not in ('cw', 'ccw'):
                raise AtomicError('INVALID_ARGUMENT', 'direction 须为 cw 或 ccw', job_id=jid)
            step['direction'] = direction
        laps = item.get('laps')
        if laps is not None:
            if type(laps) is not int or laps < 1:
                raise AtomicError('INVALID_ARGUMENT', 'laps 须为正整数', job_id=jid)
            if laps > 1 and path_mode == 'coverage':
                raise AtomicError('INVALID_ARGUMENT', 'laps > 1 仅用于边界绕飞（path_mode=boundary）', job_id=jid)
            if laps > 1 and path_mode is None and task_type == 'search':
                step['path_mode'] = 'boundary'
            step['laps'] = laps
        timeout = item.get('search_timeout')
        if timeout is not None:
            timeout = number(timeout, 'search_timeout', True)
            if task_type == 'patrol':
                raise AtomicError('INVALID_ARGUMENT', 'search_timeout 只用于搜索', job_id=jid)
            step['search_timeout'] = timeout
        jobs.append({'job_id': jid, 'name': name, 'shape': shape, 'center': center,
                     'entrance': entrance, 'fixed_uav': fixed,
                     'area_source': 'user' if set(item) & {'center_lon', 'center_lat', 'shape', 'radius', 'length', 'width'} else 'registry',
                     **sizes, **step})
    return {'jobs': jobs, 'submitted': False, '_summary': {'jobs': jobs}}
