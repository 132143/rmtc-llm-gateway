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

import rclpy
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from std_msgs.msg import String

from contestant.agent import Agent, AgentConfig
from rmtc_client import ClientConfig, LlmGatewayClient


def qos_profile():
    """协议 §2：感知/动作 RELIABLE + VOLATILE + KEEP_LAST(1)。设错会静默收不到。"""
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
    )


class RmtcBotNode(Node):
    def __init__(self, seat, stage, dry_run=False, agent_cfg=None):
        super().__init__(f"rmtc_bot_{seat}")
        self.seat = seat
        self.dry_run = dry_run
        self.log = self.get_logger()

        client = LlmGatewayClient(
            ClientConfig(
                url=os.environ.get("RMTC_AI_URL",
                                   "http://127.0.0.1:8080/v1/chat/completions"),
                token=os.environ.get("RMTC_TEAM_TOKEN", f"practice-{seat}"),
                timeout_s=1.5, min_interval_s=2.0),
            logger=lambda level, text: self.get_logger().info(f"[ai:{level}] {text}"))
        cfg = agent_cfg or AgentConfig(seat=seat, stage=stage)
        self.agent = Agent(cfg, client=client,
                           logger=lambda level, text: self.get_logger().info(text),
                           trace_path=os.environ.get("RMTC_TRACE") or None)

        qos = qos_profile()
        self.publisher = self.create_publisher(String, f"/team_{seat}/action", qos)
        self.subscription = self.create_subscription(
            String, f"/team_{seat}/perception", self.on_perception, qos)
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
    parser.add_argument("--strategy", default=os.environ.get("RMTC_STRATEGY", "greedy"),
                        choices=["greedy", "snake"])
    parser.add_argument("--combat",
                        default=os.environ.get("RMTC_COMBAT_MODE", "opportunistic"),
                        choices=["never", "opportunistic", "resource_first", "finish",
                                 "aggressive"])
    parser.add_argument("--reach-after-tick", type=int,
                        default=int(os.environ.get("RMTC_REACH_AFTER_TICK", "250")))
    args = parser.parse_args(argv)

    seat = args.seat.strip().upper()[:1] or "A"
    agent_cfg = AgentConfig(seat=seat, stage=args.stage, strategy=args.strategy,
                            combat_mode=args.combat,
                            reach_after_tick=args.reach_after_tick)
    rclpy.init()
    node = RmtcBotNode(seat, args.stage, args.dry_run, agent_cfg)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
