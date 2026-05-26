# -*- coding: utf-8 -*-
# 带 MySQL 事务的拼车匹配函数。
# 用法:
#   from carpool_matcher import match_pending_requests
#   match_pending_requests(sim_time=now_sec, base_time=SIM_BASE_TIME)
#
# 设计要点:
#   1. 每轮匹配在单个事务内完成，任一步异常立即整体回滚；
#   2. 通过 SELECT ... FOR UPDATE 锁定待匹配的乘客与候选车辆，避免并发冲突；
#   3. 即便触发器抛出 "Vehicle capacity exceeded" 也会被捕获并回滚；
#   4. 所有匹配动作同步写入 Transaction_Log，便于审计与回放。

import json
import uuid
from datetime import timedelta

from mysql.connector import Error
from carpool_db import get_connection

# 估算下车时间使用的默认行程时长（秒），用于 Matches/Current_Passengers
DEFAULT_TRIP_SECONDS = 600


def match_pending_requests(sim_time, base_time,
                           max_matches=20,
                           trip_seconds=DEFAULT_TRIP_SECONDS):
    """
    执行一轮拼车匹配，整个过程包裹在数据库事务中。

    参数:
        sim_time      : 当前仿真时间（秒）
        base_time     : 仿真起点 datetime
        max_matches   : 本轮最多匹配的请求数量
        trip_seconds  : 估算下车时间用的行程秒数

    返回: 本轮成功匹配的请求数量；异常时返回 0 并执行回滚
    """
    now = base_time + timedelta(seconds=float(sim_time))
    txn_id = uuid.uuid4().hex
    matched = 0

    conn = get_connection(autocommit=False)
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT request_id, passenger_id, origin, destination, "
            "       pickup_window_start, pickup_window_end, max_extra_stops "
            "FROM Trip_Requests "
            "WHERE status = 'pending' "
            "ORDER BY pickup_window_start ASC "
            "LIMIT %s FOR UPDATE",
            (max_matches,))
        pending = cur.fetchall()
        if not pending:
            conn.commit()
            return 0

        for req in pending:
            cur.execute(
                "SELECT vehicle_id, total_capacity, current_load "
                "FROM Vehicles "
                "WHERE status = 'available' "
                "  AND current_load < total_capacity "
                "ORDER BY current_load ASC, last_update ASC "
                "LIMIT 1 FOR UPDATE")
            veh = cur.fetchone()
            if not veh:
                break

            if veh['current_load'] >= veh['total_capacity']:
                continue

            match_id = uuid.uuid4().hex
            est_pickup = req['pickup_window_start']
            est_dropoff = est_pickup + timedelta(seconds=trip_seconds)
            cur_load = veh['current_load']
            next_seq_pickup = cur_load * 2 + 1
            next_seq_dropoff = next_seq_pickup + 1
            is_shared = cur_load > 0

            cur.execute(
                "INSERT INTO Matches "
                "(match_id, vehicle_id, request_id, "
                " estimated_pickup_time, estimated_dropoff_time, shared, "
                " pickup_stop_sequence, dropoff_stop_sequence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (match_id, veh['vehicle_id'], req['request_id'],
                 est_pickup, est_dropoff, is_shared,
                 next_seq_pickup, next_seq_dropoff))

            cur.execute(
                "UPDATE Trip_Requests SET status = 'matched' "
                "WHERE request_id = %s AND status = 'pending'",
                (req['request_id'],))
            if cur.rowcount != 1:
                raise Error("请求 %s 状态被并发修改，回滚" % req['request_id'])

            cur.execute(
                "INSERT INTO Current_Passengers "
                "(passenger_id, vehicle_id, request_id, pickup_time, "
                " pickup_location, destination, expected_dropoff_time, "
                " is_en_route, stop_sequence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)",
                (req['passenger_id'], veh['vehicle_id'], req['request_id'],
                 est_pickup, req['origin'], req['destination'], est_dropoff,
                 next_seq_pickup))

            cur.execute(
                "UPDATE Vehicles SET last_update = NOW() "
                "WHERE vehicle_id = %s",
                (veh['vehicle_id'],))

            cur.execute(
                "INSERT INTO Route_Stops "
                "(vehicle_id, location, stop_type, passenger_id, "
                " scheduled_time, stop_sequence) "
                "VALUES (%s, %s, 'pickup',  %s, %s, %s), "
                "       (%s, %s, 'dropoff', %s, %s, %s)",
                (veh['vehicle_id'], req['origin'],      req['passenger_id'],
                 est_pickup,  next_seq_pickup,
                 veh['vehicle_id'], req['destination'], req['passenger_id'],
                 est_dropoff, next_seq_dropoff))

            cur.execute(
                "INSERT INTO Transaction_Log "
                "(transaction_id, operation, table_name, record_id, new_values) "
                "VALUES (%s, 'MATCH', 'Matches', %s, %s)",
                (txn_id, match_id,
                 json.dumps({
                     'vehicle_id': veh['vehicle_id'],
                     'request_id': req['request_id'],
                     'passenger_id': req['passenger_id'],
                     'shared': is_shared,
                 })))

            matched += 1

        conn.commit()
        return matched

    except Exception as e:
        conn.rollback()
        print("[carpool_matcher] 事务回滚 (%s): %s" % (txn_id, e))
        return 0
    finally:
        cur.close()
        conn.close()
