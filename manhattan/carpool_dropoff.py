# -*- coding: utf-8 -*-
# 带 MySQL 事务的乘客下车处理函数。
# 用法:
#   from carpool_dropoff import process_dropoffs
#   process_dropoffs(sim_time=now_sec, base_time=SIM_BASE_TIME)
#
# 设计要点:
#   1. 通过 expected_dropoff_time <= now 判定哪些在车乘客已经到达目的地;
#   2. 整轮下车在单个事务内完成, 任一步异常立即整体回滚;
#   3. 通过 SELECT ... FOR UPDATE 锁定 Current_Passengers 行, 后续对
#      Vehicles 的 UPDATE 由 MySQL 自动加行锁, 避免与 matcher 并发改
#      current_load 时丢失更新;
#   4. 触发器 update_load_on_passenger_remove 在 is_en_route 由 TRUE 变 FALSE
#      时会把 Vehicles.current_load 减 1, Python 侧不再重复加减;
#   5. 同步把 Route_Stops 对应 dropoff 标记 completed, Trip_Requests 切到
#      completed, 并写 Transaction_Log 留痕。

import json
import uuid
from datetime import timedelta

from mysql.connector import Error
from carpool_db import get_connection


def process_dropoffs(sim_time, base_time, max_dropoffs=50):
    """
    处理一轮乘客下车,所有写动作在同一个事务内完成。

    参数:
        sim_time     : 当前仿真时间(秒)
        base_time    : 仿真起点 datetime
        max_dropoffs : 本轮最多处理的下车数量

    返回: 本轮成功下车的乘客数量;异常时返回 0 并执行回滚
    """
    now = base_time + timedelta(seconds=float(sim_time))
    txn_id = uuid.uuid4().hex
    dropped = 0

    conn = get_connection(autocommit=False)
    cur = conn.cursor(dictionary=True)
    try:
        # 锁定所有"已到目的地"的在车乘客
        cur.execute(
            "SELECT passenger_id, vehicle_id, request_id, destination "
            "FROM Current_Passengers "
            "WHERE is_en_route = TRUE "
            "  AND expected_dropoff_time IS NOT NULL "
            "  AND expected_dropoff_time <= %s "
            "ORDER BY expected_dropoff_time ASC "
            "LIMIT %s FOR UPDATE",
            (now, max_dropoffs))
        passengers = cur.fetchall()
        if not passengers:
            conn.commit()
            return 0

        for p in passengers:
            # 1) 把乘客标记为已下车;
            #    触发器 update_load_on_passenger_remove 会自动 current_load -= 1
            cur.execute(
                "UPDATE Current_Passengers "
                "SET is_en_route = FALSE, actual_dropoff_time = %s "
                "WHERE passenger_id = %s AND vehicle_id = %s "
                "  AND is_en_route = TRUE",
                (now, p['passenger_id'], p['vehicle_id']))
            if cur.rowcount != 1:
                raise Error("乘客 %s 状态被并发修改, 回滚" % p['passenger_id'])

            # 2) 同名乘客在 Route_Stops 中对应 dropoff 节点标记完成
            cur.execute(
                "UPDATE Route_Stops "
                "SET completed = TRUE, actual_time = %s "
                "WHERE vehicle_id = %s AND passenger_id = %s "
                "  AND stop_type = 'dropoff' AND completed = FALSE",
                (now, p['vehicle_id'], p['passenger_id']))

            # 3) 行程请求切到 completed; 用 status='matched' 作为乐观锁守卫
            cur.execute(
                "UPDATE Trip_Requests SET status = 'completed' "
                "WHERE request_id = %s AND status = 'matched'",
                (p['request_id'],))

            # 4) 刷新 Vehicles.last_update, 让车辆能被下一轮 matcher 优先选中;
            #    若车辆是 'offline' 则不动它, 其它情况一律保证 'available'
            #    (matcher 的过滤条件就是 status='available' AND current_load<cap)
            cur.execute(
                "UPDATE Vehicles "
                "SET last_update = NOW(), "
                "    status = CASE WHEN status = 'offline' THEN status "
                "                  ELSE 'available' END "
                "WHERE vehicle_id = %s",
                (p['vehicle_id'],))

            # 5) 留痕审计
            cur.execute(
                "INSERT INTO Transaction_Log "
                "(transaction_id, operation, table_name, record_id, new_values) "
                "VALUES (%s, 'DROPOFF', 'Current_Passengers', %s, %s)",
                (txn_id, p['passenger_id'],
                 json.dumps({
                     'vehicle_id': p['vehicle_id'],
                     'request_id': p['request_id'],
                     'destination': p['destination'],
                     'dropoff_time': now.isoformat(sep=' '),
                 })))

            dropped += 1

        conn.commit()
        return dropped

    except Exception as e:
        conn.rollback()
        print("[carpool_dropoff] 事务回滚 (%s): %s" % (txn_id, e))
        return 0
    finally:
        cur.close()
        conn.close()
