"""WGS84 direct geodesic calculation, shared numeric primitive."""
from math import atan2, cos, degrees, pi, radians, sin, sqrt, tan
from typing import Tuple

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)
def vincenty_direct(
    lon: float,
    lat: float,
    bearing_deg: float,
    distance_m: float,
) -> Tuple[float, float]:
    """WGS84 大地线正解：给定起点、方位角、距离求终点经纬度。"""

    phi1 = radians(lat)
    lambda1 = radians(lon)
    alpha1 = radians(bearing_deg)

    tan_u1 = (1 - WGS84_F) * tan(phi1)
    cos_u1 = 1 / sqrt(1 + tan_u1 ** 2)
    sin_u1 = tan_u1 * cos_u1

    sigma1 = atan2(tan_u1, cos(alpha1))
    sin_alpha = cos_u1 * sin(alpha1)
    cos_sq_alpha = 1 - sin_alpha ** 2
    u_sq = cos_sq_alpha * (WGS84_A ** 2 - WGS84_B ** 2) / WGS84_B ** 2

    a = 1 + u_sq / 16384 * (
        4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq))
    )
    b = u_sq / 1024 * (
        256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq))
    )

    sigma = distance_m / (WGS84_B * a)

    converged = False
    for _ in range(200):
        cos_2sm = cos(2 * sigma1 + sigma)
        sin_s, cos_s = sin(sigma), cos(sigma)
        delta = (
            b
            * sin_s
            * (
                cos_2sm
                + b / 4
                * (
                    cos_s * (-1 + 2 * cos_2sm ** 2)
                    - b / 6
                    * cos_2sm
                    * (-3 + 4 * sin_s ** 2)
                    * (-3 + 4 * cos_2sm ** 2)
                )
            )
        )
        sigma_new = distance_m / (WGS84_B * a) + delta
        if abs(sigma_new - sigma) < 1e-12:
            sigma = sigma_new
            converged = True
            break
        sigma = sigma_new

    if not converged:
        raise RuntimeError("Vincenty 迭代未收敛")

    sin_s, cos_s = sin(sigma), cos(sigma)
    cos_2sm = cos(2 * sigma1 + sigma)
    tmp = sin_u1 * sin_s - cos_u1 * cos_s * cos(alpha1)

    phi2 = atan2(
        sin_u1 * cos_s + cos_u1 * sin_s * cos(alpha1),
        (1 - WGS84_F) * sqrt(sin_alpha ** 2 + tmp ** 2),
    )

    lambda_delta = atan2(
        sin_s * sin(alpha1),
        cos_u1 * cos_s - sin_u1 * sin_s * cos(alpha1),
    )

    c = WGS84_F / 16 * cos_sq_alpha * (
        4 + WGS84_F * (4 - 3 * cos_sq_alpha)
    )

    L = lambda_delta - (1 - c) * WGS84_F * sin_alpha * (
        sigma + c * sin_s * (
            cos_2sm + c * cos_s * (-1 + 2 * cos_2sm ** 2)
        )
    )

    lambda2 = (lambda1 + L + 3 * pi) % (2 * pi) - pi

    # Preserve sub-metre offsets in the campus scene; rounding to six decimals
    # moved even a due-north result sideways by several centimetres.
    return round(degrees(lambda2), 9), round(degrees(phi2), 9)

