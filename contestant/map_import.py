"""裁判地图编辑器的格式互通（导入 / 导出 / 校验）。

背景：主办方给了裁判用的单文件编辑器（地图编辑器.html），导出的 JSON 有两档：
  · 正式地图（geometry）：只含 {"map": {width,height,obstacles,resources}, "robots":[{"team","spawn"}]}
  · 测试完整配置（practice）：在 geometry 基础上带 stage / vision_radius / missions / events

我们为什么要这一步：
  1) **用裁判自己的地图做练习**——不再只用随机图，可以直接把主办方/裁判编的地图丢进实验台；
  2) **反向**把我们造的压力图导出成编辑器格式，队友可以用鼠标改两下再跑，不用改代码；
  3) 顺带把编辑器的校验规则抄进来（尺寸、重复资源号、重叠、出生点席位、reach 不能压在障碍上），
     避免"我们以为合法、裁判导入时报错"。

坐标约定（与编辑器、规则一致）：[x, y]，左上角 [0,0]，x 向右、y 向下；第 2 阶段任务是 [12,12]，
因此**地图至少要 13×13，且 [12,12] 不能是障碍**（编辑器会据此警告）。
"""

from __future__ import annotations

import json
import os

MAX_SIDE = 200
FORMAL_IMPORT_LIMIT = 100 * 1024      # 正式地图导入上限 100 KiB（编辑器提示）
REACH_DEFAULT = (12, 12)


class MapFormatError(ValueError):
    """地图格式不合法（消息直接给人看，不要吞掉）。"""


def _is_point(value, width, height):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            and 0 <= value[0] < width and 0 <= value[1] < height)


def parse_editor(payload):
    """把编辑器导出的 JSON（正式地图或测试完整配置）解析成标准结构。

    返回 dict：
        {"width","height","stage","obstacles":set,"resources":{pos: value},
         "spawns":{seat: pos},"reach":(x,y) 或 None,"missions":[...],"events":{...}}
    """
    if not isinstance(payload, dict):
        raise MapFormatError("顶层必须是 JSON 对象")
    if "event_params_file" in payload:
        raise MapFormatError("该配置引用了外部 event_params_file，编辑器也不支持；请用事件已内嵌的完整配置")
    game_map = payload.get("map")
    robots = payload.get("robots")
    if not isinstance(game_map, dict) or not isinstance(robots, list):
        raise MapFormatError("必须含 map 与 robots（编辑器的导出结构）")

    width, height = game_map.get("width"), game_map.get("height")
    if not (isinstance(width, int) and isinstance(height, int)
            and not isinstance(width, bool) and not isinstance(height, bool)):
        raise MapFormatError("map.width / map.height 必须是整数")
    if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE):
        raise MapFormatError("地图宽高须为 1–{0}".format(MAX_SIDE))

    obstacles_in = game_map.get("obstacles") or []
    resources_in = game_map.get("resources") or []
    if not isinstance(obstacles_in, list) or not isinstance(resources_in, list):
        raise MapFormatError("obstacles / resources 必须是数组")

    obstacles = set()
    for point in obstacles_in:
        if not _is_point(point, width, height):
            raise MapFormatError("障碍坐标越界或格式无效：{0!r}".format(point))
        obstacles.add((point[0], point[1]))

    resources = {}
    ids = set()
    for item in resources_in:
        if not isinstance(item, dict):
            raise MapFormatError("资源条目必须是对象")
        rid = item.get("id")
        if not isinstance(rid, str) or not rid:
            raise MapFormatError("资源必须有非空字符串 id")
        if rid in ids:
            raise MapFormatError("资源 id 重复：{0}".format(rid))
        ids.add(rid)
        point = item.get("position")
        if not _is_point(point, width, height):
            raise MapFormatError("资源坐标越界或格式无效：{0!r}".format(point))
        value = item.get("value")
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            raise MapFormatError("资源 value 必须是非负整数")
        cell = (point[0], point[1])
        if cell in obstacles or cell in resources:
            raise MapFormatError("资源 {0} 与障碍或其他资源重合".format(rid))
        resources[cell] = int(value)

    stage = payload.get("stage")
    if stage is None:
        stage = 1 if len(robots) == 1 else 2
    if stage not in (1, 2):
        raise MapFormatError("只支持第一阶段（1）和第二阶段（2）")

    expected = ("A",) if stage == 1 else ("A", "B", "C", "D")
    spawns = {}
    occupied = set()
    for item in robots:
        if not isinstance(item, dict):
            raise MapFormatError("robots 条目必须是对象")
        team = item.get("team")
        if team not in expected:
            raise MapFormatError("席位 {0!r} 不合法（阶段 {1} 只允许 {2}）".format(team, stage, "/".join(expected)))
        if team in spawns:
            raise MapFormatError("席位 {0} 出生点重复".format(team))
        point = item.get("spawn")
        if not _is_point(point, width, height):
            raise MapFormatError("席位 {0} 出生点越界或格式无效".format(team))
        cell = (point[0], point[1])
        if cell in occupied:
            raise MapFormatError("出生点不能重合：{0}".format(cell))
        if cell in obstacles:
            raise MapFormatError("出生点不能压在障碍上：{0}".format(cell))
        spawns[team] = cell
        occupied.add(cell)
    missing = [team for team in expected if team not in spawns]
    if missing:
        raise MapFormatError("缺少出生点：{0}".format("/".join(missing)))

    missions = payload.get("missions") or []
    reach = None
    if isinstance(missions, list):
        for mission in missions:
            if not isinstance(mission, dict) or mission.get("type") != "reach":
                continue
            position = mission.get("position")
            if not _is_point(position, width, height):
                raise MapFormatError("reach 任务坐标越界：{0!r}".format(position))
            if (position[0], position[1]) in obstacles:
                raise MapFormatError("reach 任务压在障碍上：{0}".format(tuple(position)))
            reach = (position[0], position[1])
    if reach is None and stage == 2:
        # 正式第二阶段保留到达任务 [12,12]（编辑器相同提示）→ 地图至少 13×13 且该格无碍
        if width < 13 or height < 13:
            raise MapFormatError("第二阶段地图宽高至少 13（任务点 [12,12] 需在地图内），当前 {0}×{1}".format(width, height))
        if REACH_DEFAULT in obstacles:
            raise MapFormatError("任务点 [12,12] 不能是障碍（编辑器会拒绝导入）")
        reach = REACH_DEFAULT

    return {"width": width, "height": height, "stage": stage,
            "obstacles": obstacles, "resources": resources,
            "spawns": spawns, "reach": reach,
            "missions": missions if isinstance(missions, list) else [],
            "events": payload.get("events") or {"enabled": False}}


def build_map(payload):
    """解析并构造 Stage2Map（延迟导入，避免循环依赖）。"""
    from contestant.stage2_sim import Stage2Map
    parsed = parse_editor(payload)
    return Stage2Map(parsed["width"], parsed["height"], parsed["obstacles"],
                     [{"position": list(pos), "value": value}
                      for pos, value in parsed["resources"].items()],
                     parsed["spawns"],
                     reach=parsed["reach"] or REACH_DEFAULT)


def from_editor(payload):
    return build_map(payload)


def load_editor_file(path):
    """读文件并做"正式导入 100 KiB 上限"的检查。"""
    if not os.path.exists(path):
        raise MapFormatError("找不到地图文件：{0}".format(path))
    size = os.path.getsize(path)
    if size > FORMAL_IMPORT_LIMIT:
        raise MapFormatError("文件 {0} 超过正式导入上限 100 KiB（{1} 字节）".format(path, size))
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return build_map(payload)


def to_editor(game_map, stage=2):
    """把我们的地图导出成编辑器格式（队友可以用鼠标打开改）。"""
    obstacles = sorted([list(cell) for cell in game_map.obstacles])
    resources = [{"id": "{0}_{1}_{2}".format("high" if value >= 20 else "normal", pos[0], pos[1]),
                  "position": [pos[0], pos[1]], "value": int(value)}
                 for pos, value in sorted(game_map.resources.items())]
    robots = [{"team": seat, "spawn": [pos[0], pos[1]]}
              for seat, pos in sorted(game_map.spawns.items())]
    return {"stage": stage,
            "map": {"width": game_map.width, "height": game_map.height,
                    "obstacles": obstacles, "resources": resources},
            "robots": robots}


def to_practice_config(game_map, stage=2, missions=None, event_text=""):
    """导出成"测试完整配置"，可直接被编辑器/测试裁判导入。"""
    payload = to_editor(game_map, stage=stage)
    payload.update({
        "vision_radius": 4,
        "respawn_wait": 3,
        "spawn_protection": 2,
        "mission_text": "累计收集至少 2 个普通资源；累计收集至少 1 个高价值资源；"
                        "在比赛结束前至少一次到达 [{0},{1}]".format(*(game_map.reach or REACH_DEFAULT)),
        "missions": missions if missions is not None else [
            {"id": "collect_two_normal", "type": "normal_resources", "target": 2},
            {"id": "collect_high_value", "type": "high_resources", "target": 1},
            {"id": "reach_target", "type": "reach",
             "position": list(game_map.reach or REACH_DEFAULT)},
        ],
        "events": {"enabled": bool(event_text), "announcements": [event_text] if event_text else []},
    })
    return payload
