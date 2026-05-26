# -*- coding: utf-8 -*-
# 乘客行程请求自动生成器。
# 用法（在 runner.py 或独立脚本中）：
#   from passenger_generator import generate_trip_requests
#   generate_trip_requests(sim_time=now_sec,
#                          base_time=SIM_BASE_TIME,
#                          net_file='data/net.net.xml',
#                          batch_size=20)

import os
import random
import uuid
import xml.etree.ElementTree as ET
from datetime import timedelta

from carpool_db import get_connection

# 路网道路 ID 缓存，避免每轮重复解析 net.net.xml
_EDGE_CACHE = None


def _load_edges(net_file):
    """解析 SUMO 路网，返回普通车道道路 ID 列表（排除内部节点边）。"""
    global _EDGE_CACHE
    if _EDGE_CACHE is not None:
        return _EDGE_CACHE
    if not os.path.exists(net_file):
        raise FileNotFoundError("找不到 SUMO 路网文件: %s" % net_file)
    edges = []
    for edge in ET.parse(net_file).getroot().findall('edge'):
        # 跳过路口内部边
        if edge.get('function') == 'internal':
            continue
        eid = edge.get('id')
        if eid:
            edges.append(eid)
    if len(edges) < 2:
        raise RuntimeError("路网道路数量不足，无法生成请求")
    _EDGE_CACHE = edges
    return edges


def generate_trip_requests(sim_time, base_time, net_file,
                           batch_size=10,
                           window_start_range=(0, 300),
                           window_length_range=(60, 600),
                           max_extra_stops_range=(0, 3)):
    """
    批量生成乘客拼车请求并写入 Trip_Requests 表，状态默认 pending。

    参数:
        sim_time            : 当前仿真时间（秒），如 traci.simulation.getTime()
        base_time           : 仿真起始时刻 datetime，对应 runner.py 的 SIM_BASE_TIME
        net_file            : SUMO 路网文件路径，用于抽样随机起止道路
        batch_size          : 本轮生成的请求数量
        window_start_range  : 上车时间窗口起点相对当前的随机偏移区间（秒）
        window_length_range : 上车时间窗口长度的随机区间（秒）
        max_extra_stops_range: 可接受额外停靠次数的随机区间

    返回: 成功写入的请求数量
    """
    edges = _load_edges(net_file)
    now = base_time + timedelta(seconds=float(sim_time))

    rows = []
    for _ in range(batch_size):
        # 随机抽取不同的起止道路
        origin, destination = random.sample(edges, 2)
        # 随机的上车时间窗口
        start_offset = random.randint(*window_start_range)
        window_len = random.randint(*window_length_range)
        pickup_start = now + timedelta(seconds=start_offset)
        pickup_end = pickup_start + timedelta(seconds=window_len)
        # 随机可接受的额外停靠次数
        max_extra = random.randint(*max_extra_stops_range)

        rows.append((
            uuid.uuid4().hex,                         # request_id
            'p_' + uuid.uuid4().hex[:12],             # passenger_id
            origin, destination,
            pickup_start, pickup_end,
            max_extra,
        ))

    # 批量插入，使用普通连接的隐式事务，整批写入或整批失败
    conn = get_connection(autocommit=False)
    try:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO Trip_Requests "
            "(request_id, passenger_id, origin, destination, "
            " pickup_window_start, pickup_window_end, max_extra_stops, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')",
            rows)
        inserted = cur.rowcount
        conn.commit()
        return inserted
    except Exception as e:
        conn.rollback()
        print("[passenger_generator] 写入失败已回滚: %s" % e)
        return 0
    finally:
        cur.close()
        conn.close()
