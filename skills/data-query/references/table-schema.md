# 扩展表字段

仅常用字段表没有所需字段，或用户明确查询型号、悬停、垂直速度、环境性能、重量或探测距离时读。不枚举数据库结构、不把字段跨表使用；常用字段与查询规则见 [主入口](../SKILL.md)。

## uav_resource：静态性能补充

| 字段 | 说明 |
|---|---|
| `uav_model`、`uav_series` | 型号、系列 |
| `max_endurance`、`hover_endurance` | 最大续航、悬停续航，单位 min；时间预算只用 `max_endurance`，悬停未进预算 |
| `max_up_speed`、`max_down_speed` | 垂直速度，m/s；当前未进预算 |
| `max_wind_speed`、`emi_resistance_level` | 抗风、抗电磁干扰；当前只查询，不判可行 |
| `takeoff_mode`、`hover_accuracy`、`max_angular_velocity` | 起飞模式、悬停精度、最大角速度；当前只查询 |
| `max_operating_temp`、`min_operating_temp`、`min_takeoff_landing_distance` | 温度限制、最小起降距离；当前只查询/未用，不判可行 |

## uav_status：状态记录补充

| 字段 | 说明 |
|---|---|
| `uav_weight`、`empty_weight`、`full_weight`、`load_coff` | 造库时推 `flight_remain`；runtime 不直接读 |
| `max_range_full`、`max_range_empty` | 满载/空载航程，m；造库时用于推 `flight_remain` |
| `load_range` | 机载载荷的最小**探测距离**，m；**不是航程**，不要用于距离预算 |
| `camera_recognition_range`、`infrared_detection_range`、`lidar_detection_range` | 各类探测距离；只证明登记能力，不证明具体识别 |

探测距离只表示登记参数，不证明具体类别、外观或识别效果；未知能力保留为未验证。预算由候选工具计算，不自行把探测距离用作航程。
