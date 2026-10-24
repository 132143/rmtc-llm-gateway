#!/usr/bin/env python3
"""ROS 2 节点：把 agent.py 接到真实话题上（只有这一层依赖 rclpy）。

用法（虚拟机里）：
    source /opt/ros/humble/setup.bash
    export ROS_DOMAIN_ID=27
    export ROS_LOCALHOST_ONLY=0
    export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
    export RMTC_SEAT=A
    export RMTC_TEAM_TOKEN=practice-A
    export RMTC_AI_URL=http://127.0.0.1:8000/v1/chat/completions
    python3 -m contestant.bot_node

参数：
    --seat A        本场席位
    --stage 1|2     第一阶段（只能走/等）或第二阶段（可打/防）
    --dry-run       不发布动作，只打印（用来确认解析没崩）

注意：这个文件在本仓库的测试里没有跑过（Windows 上没有 ROS 2），
逻辑本体在 agent.py，那边有测试覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
except ImportError:                      # Windows 上通常没有 ROS 2：给出可读提示而不是堆栈
    sys.stderr.write(
        "这个节点需要 ROS 2（rclpy），一般只在 Ubuntu + ROS 2 Humble 上运行。\n"
        "  · 只想验证逻辑：在任意系统跑  python run_tests.py\n"
        "  · 想在 Windows 上跑本地网关：python -m rmtc_gateway --port 8000\n"
        "  · 要真上场：在 Ubuntu 里 source /opt/ros/humble/setup.bash 后再运行\n")
    raise SystemExit(3)
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from std_msgs.msg import String

from contestant.agent import Agent, AgentConfig
from contestant.decision import DECISION_TYPES, load_decision_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_decision_config(path):
    """找 decision_config.json：显式路径 → 当前目录 → 仓库根目录。

    不写死任何绝对路径：队友把整个仓库放到任意目录（Windows/Linux 都行）都能用。
    """
    if path:
        if os.path.isabs(path) or os.path.exists(path):
            return path
    candidates = [path] if path else []
    candidates += ["decision_config.json", os.path.join(REPO_ROOT, "decision_config.json")]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return path or "decision_config.json"
from rmtc_client import ClientConfig, LlmGatewayClient


def qos_profile(durability=DurabilityPolicy.VOLATILE):
    """协议 §2：感知/动作 RELIABLE + VOLATILE；注册用 RELIABLE + TRANSIENT_LOCAL + 深度 1。"""
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=durability,
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
    )


# 规则 §2.3：现场唯一允许切换的是 decision_config.json 里的 decision_type，
# 取值必须是赛前封存并向裁判登记过的枚举。登记表在 contestant/decision.py。


class RmtcBotNode(Node):
    def __init__(self, seat, stage, dry_run=False, agent_cfg=None, team_name=None):
        super().__init__(f"rmtc_bot_{seat}")
        self.seat = seat
        self.dry_run = dry_run
        self.team_name = team_name or f"队伍{seat}"
        self.log = self.get_logger()

        client = LlmGatewayClient(
            ClientConfig(
                url=os.environ.get("RMTC_AI_URL",
                                   "http://127.0.0.1:8080/v1/chat/completions"),
                token=os.environ.get("RMTC_TEAM_TOKEN", f"practice-{seat}"),
                timeout_s=4.0, min_interval_s=0.8),
            logger=lambda level, text: self.get_logger().info(f"[ai:{level}] {text}"))
        cfg = agent_cfg or AgentConfig(seat=seat, stage=stage)
        self.agent = Agent(cfg, client=client,
                           logger=lambda level, text: self.get_logger().info(text),
                           trace_path=os.environ.get("RMTC_TRACE") or None)

        qos = qos_profile()
        self.publisher = self.create_publisher(String, f"/team_{seat}/action", qos)
        self.subscription = self.create_subscription(
            String, f"/team_{seat}/perception", self.on_perception, qos)
        # 协议 §12.1：队伍注册话题由选手发布（只改显示名称，不影响身份与权限）。
        self.registration = self.create_publisher(
            String, f"/team_{seat}/registration", qos_profile(DurabilityPolicy.TRANSIENT_LOCAL))
        self.create_timer(5.0, self.publish_registration)
        self.publish_registration()
        # 失联看门狗：现场最常见的坑是"以为连上了"，其实是 QoS/域号/发现没通。
        self.last_perception_at = None
        self.started_at = self.get_clock().now()
        self.create_timer(2.0, self.watchdog)
        self.get_logger().info(
            f"席位 {seat} 就绪：订阅 /team_{seat}/perception，发布 /team_{seat}/action"
            f"（stage {stage}，策略 {self.agent.cfg.strategy}，战斗 {self.agent.cfg.combat_mode}，"
            f"reach_after_tick {self.agent.cfg.reach_after_tick}）")

    def watchdog(self):
        now = self.get_clock().now()
        if os.environ.get("RMTC_WATCHDOG_DEBUG"):
            started = (now - self.started_at).nanoseconds / 1e9
            self.get_logger().info(f"[watchdog] tick at {started:.1f}s, "
                                   f"last_perception={self.last_perception_at}")
        if self.last_perception_at is None:
            if (now - self.started_at).nanoseconds / 1e9 > 5.0:
                self.get_logger().warn(
                    "5 秒没收到任何感知：检查 ROS_DOMAIN_ID / ROS_LOCALHOST_ONLY=0 / "
                    "RMW_IMPLEMENTATION / 席位是否选对 / 裁判是否已开局")
                self.started_at = now          # 每 5 秒提醒一次，别刷屏
            return
        gap = (now - self.last_perception_at).nanoseconds / 1e9
        if gap > 3.0:
            self.get_logger().warn(f"已经 {gap:.1f} 秒没有新感知（比赛暂停？网络断了？）")
            self.last_perception_at = now

    def publish_registration(self):
        message = String()
        message.data = json.dumps({"team": self.seat, "name": self.team_name}, ensure_ascii=False)
        self.registration.publish(message)

    def on_perception(self, msg):
        self.last_perception_at = self.get_clock().now()
        try:
            data = json.loads(msg.data)
        except (TypeError, ValueError) as exc:
            self.get_logger().warn(f"感知不是合法 JSON：{exc}")
            return
        try:
            action = self.agent.on_perception(data)
        except Exception as exc:                      # 协议要求：解析异常不能让进程退出
            self.get_logger().error(f"决策异常（本 Tick 记 wait）：{exc}")
            return
        if action is None:
            return
        if self.dry_run:
            self.get_logger().info(f"[dry-run] {json.dumps(action, ensure_ascii=False)}")
            return
        message = String()
        message.data = json.dumps(action, ensure_ascii=False)
        self.publisher.publish(message)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--seat", default=os.environ.get("RMTC_SEAT", "A"))
    parser.add_argument("--stage", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    # 规则 §2.3：现场切换策略只能改 decision_config.json 里的 decision_type。
    # 下面两个参数是给"赛前练习/对照实验"用的，正式场次建议只用 --decision-config。
    parser.add_argument("--decision-config", default=None,
                        help="decision_config.json 的路径；默认从当前目录和仓库根目录自动找")
    parser.add_argument("--strategy", default=None, choices=["greedy", "snake"])
    parser.add_argument("--combat", default=None,
                        choices=["never", "opportunistic", "resource_first", "finish",
                                 "hybrid", "aggressive"])
    parser.add_argument("--reach-after-tick", type=int,
                        default=int(os.environ.get("RMTC_REACH_AFTER_TICK", "250")))
    parser.add_argument("--team-name", default=os.environ.get("RMTC_TEAM_NAME"))
    # 赛前调参用（封存后这些值固定；现场只允许改 decision_config.json）
    parser.add_argument("--detour", type=float,
                        default=float(os.environ.get("RMTC_DETOUR", "0")))
    parser.add_argument("--mission-max-distance", type=int,
                        default=int(os.environ.get("RMTC_MISSION_MAX_DIST", "8")))
    parser.add_argument("--no-patrol", action="store_true",
                        help="关闭空闲巡逻（对照实验用）")
    parser.add_argument("--opening-mode", default=os.environ.get("RMTC_OPENING_MODE", "normal"),
                        choices=["normal", "nearest", "high_first"],
                        help="开局微策略（实验用）")
    parser.add_argument("--opening-ticks", type=int,
                        default=int(os.environ.get("RMTC_OPENING_TICKS", "30")))
    args = parser.parse_args(argv)

    seat = args.seat.strip().upper()[:1] or "A"
    config_path = resolve_decision_config(args.decision_config)
    decision_type, note = load_decision_config(config_path)
    chosen = dict(DECISION_TYPES[decision_type])
    if args.strategy:
        chosen["strategy"] = args.strategy
    if args.combat:
        chosen["combat_mode"] = args.combat
    agent_cfg = AgentConfig(seat=seat, stage=args.stage,
                            strategy=chosen["strategy"], combat_mode=chosen["combat_mode"],
                            reach_after_tick=args.reach_after_tick,
                            strategy_detour=args.detour,
                            mission_max_distance=args.mission_max_distance,
                            patrol_when_idle=not args.no_patrol,
                            opening_mode=args.opening_mode,
                            opening_ticks=args.opening_ticks)
    rclpy.init()
    node = RmtcBotNode(seat, args.stage, args.dry_run, agent_cfg, team_name=args.team_name)
    node.get_logger().info(
        f"决策类型：{decision_type}（{note}）；配置文件 {config_path} → strategy={chosen['strategy']}, "
        f"combat={chosen['combat_mode']}；队伍名 {node.team_name}")
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass                     # Ctrl-C / timeout / SIGTERM 都是正常收场，不必打 traceback
    finally:
        try:
            node.destroy_node()
            rclpy.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
