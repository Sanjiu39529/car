#!/usr/bin/env python
# Eclipse SUMO, Simulation of Urban MObility; see https://eclipse.dev/sumo
# Copyright (C) 2008-2026 German Aerospace Center (DLR) and others.
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# https://www.eclipse.org/legal/epl-2.0/
# This Source Code may also be made available under the following Secondary
# Licenses when the conditions for such availability set forth in the Eclipse
# Public License 2.0 are satisfied: GNU General Public License, version 2
# or later which is available at
# https://www.gnu.org/licenses/old-licenses/gpl-2.0-standalone.html
# SPDX-License-Identifier: EPL-2.0 OR GPL-2.0-or-later

# @file    runner.py
# @author  Daniel Krajzewicz
# @author  Michael Behrisch
# @date    2007-10-25

from __future__ import absolute_import
from __future__ import print_function

import os
import sys
import xml.etree.ElementTree as ET
from subprocess import call
from datetime import datetime, timedelta

import mysql.connector
from mysql.connector import Error

try:
    sys.path.append(os.path.join(os.path.dirname(
        __file__), '..', '..', '..', '..', "tools"))  # tutorial in tests
    sys.path.append(os.path.join(os.environ.get("SUMO_HOME", os.path.join(
        os.path.dirname(__file__), "..", "..", "..")), "tools"))  # tutorial in docs
    from sumolib import checkBinary  # noqa
except ImportError:
    sys.exit("please declare environment variable 'SUMO_HOME'")

import randomTrips  # noqa
import traci  # noqa

# 拼车业务模块
from carpool_db import (
    get_connection,
    ensure_alive,
    is_disconnect_error,
    DISCONNECT_ERRNOS,
)
from passenger_generator import generate_trip_requests
from carpool_matcher import match_pending_requests
from carpool_dropoff import process_dropoffs

# =====================================================================
# 乘客可视化模块 —— 在 SUMO GUI 上以行人标记显示 Trip_Requests
# =====================================================================

# 当前已添加到 SUMO 中的乘客 person_id 集合及其状态
_active_persons = {}          # { person_id: 'pending' | 'matched' }
_person_request_map = {}      # { person_id: request_id }
_matched_fade_counter = {}    # { person_id: 剩余显示步数 }

MATCHED_DISPLAY_STEPS = 5     # 匹配成功后保留几步再移除（方便用户看到颜色变化）

COLOR_PENDING  = (255, 200, 0, 255)    # 黄色 — 等待中
COLOR_MATCHED  = (0, 200, 0, 255)      # 绿色 — 已匹配


def add_passenger_markers(db_cursor):
    """
    从数据库读取所有 pending 的 Trip_Requests，
    对还未在 SUMO 中创建 person 的请求，用 traci.person.add() 在其 origin 边上生成行人。
    """
    db_cursor.execute(
        "SELECT request_id, passenger_id, origin "
        "FROM Trip_Requests WHERE status = 'pending'")
    rows = db_cursor.fetchall()

    existing_persons = set(traci.person.getIDList())

    for row in rows:
        req_id = row[0]
        pid = row[1]
        origin_edge = row[2]

        if pid in _active_persons:
            continue

        if pid in existing_persons:
            continue

        try:
            traci.person.add(pid, origin_edge, pos=5.0)
            traci.person.appendWaitingStage(pid, duration=1e6)
            traci.person.setColor(pid, COLOR_PENDING)
            traci.person.setLength(pid, 0.5)
            traci.person.setWidth(pid, 0.5)
            _active_persons[pid] = 'pending'
            _person_request_map[pid] = req_id
        except traci.TraCIException:
            pass


def update_passenger_markers(db_cursor):
    """
    查询数据库中已匹配的请求，将对应 person 的颜色改为绿色，
    并启动淡出计时器。
    """
    if not _active_persons:
        return

    pending_pids = [pid for pid, st in _active_persons.items() if st == 'pending']
    if not pending_pids:
        return

    req_ids = [_person_request_map[pid] for pid in pending_pids]
    placeholders = ','.join(['%s'] * len(req_ids))
    db_cursor.execute(
        "SELECT request_id FROM Trip_Requests "
        "WHERE request_id IN (%s) AND status = 'matched'" % placeholders,
        tuple(req_ids))
    matched_req_ids = {r[0] for r in db_cursor.fetchall()}

    for pid in pending_pids:
        rid = _person_request_map[pid]
        if rid in matched_req_ids:
            _active_persons[pid] = 'matched'
            _matched_fade_counter[pid] = MATCHED_DISPLAY_STEPS
            try:
                traci.person.setColor(pid, COLOR_MATCHED)
            except traci.TraCIException:
                pass


def remove_matched_passengers():
    """
    将已匹配且淡出计时结束的 person 从 SUMO 中移除。
    """
    to_remove = []
    for pid, remaining in list(_matched_fade_counter.items()):
        remaining -= 1
        if remaining <= 0:
            to_remove.append(pid)
        else:
            _matched_fade_counter[pid] = remaining

    for pid in to_remove:
        try:
            traci.person.remove(pid)
        except traci.TraCIException:
            pass
        _active_persons.pop(pid, None)
        _person_request_map.pop(pid, None)
        _matched_fade_counter.pop(pid, None)


PASSENGER_SYNC_INTERVAL = 20  # 每 20 步同步一次

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY_FILE = os.path.join(HERE, 'data', 'summary.xml')
TRIPINFO_FILE = os.path.join(HERE, 'data', 'tripinfo.xml')
VEHROUTES_FILE = os.path.join(HERE, 'data', 'vehroutes.xml')
NET_FILE = os.path.join(HERE, 'data', 'net.net.xml')

SIM_DURATION = 7200
SIM_BASE_TIME = datetime(2026, 1, 1, 0, 0, 0)

# 拼车业务参数
INITIAL_REQUESTS = 3000   # 仿真开始前预生成的乘客请求数
MATCH_INTERVAL = 20        # 每隔多少个仿真步执行一轮匹配
DROPOFF_INTERVAL = 20      # 每隔多少个仿真步检查一次乘客下车
VEHICLE_CAPACITY = 4       # 写入 Vehicles 表时的默认总座位数

DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'root',
    'password': '123456',
    'database': 'carpool',
}


def _reset_carpool_tables():
    """清空拼车业务表，确保每次仿真从干净状态开始。"""
    conn = get_connection(autocommit=False)
    cur = conn.cursor()
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for tbl in ('Route_Stops', 'Current_Passengers', 'Matches',
                     'Trip_Requests', 'Transaction_Log', 'Vehicles',
                     'Performance_Metrics'):
            cur.execute("TRUNCATE TABLE " + tbl)
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        print("[init] 拼车业务表已清空")
    except Exception as e:
        conn.rollback()
        print("[init] 清表失败: %s" % e)
    finally:
        cur.close()
        conn.close()


def run_simulation():
    netgenBinary = checkBinary('netgenerate')
    jtrrouterBinary = checkBinary('jtrrouter')
    sumoBinary = checkBinary('sumo-gui')

    # ---- 清空上一轮残留数据 ----
    _reset_carpool_tables()

    # ---- 路网与车流生成（保持原状）----
    call([netgenBinary, '-c', 'data/manhattan.netgcfg'])
    randomTrips.main(randomTrips.get_options([
        '--flows', '500',
        '-b', '0',
        '-e', '1',
        '-n', 'data/net.net.xml',
        '-o', 'data/flows.xml',
        '--jtrrouter',
        '--trip-attributes', 'departPos="random" departSpeed="max"']))
    call([jtrrouterBinary, '-c', 'data/manhattan.jtrrcfg'])

    # ---- 仿真开始前预生成乘客请求 ----
    try:
        n = generate_trip_requests(
            sim_time=0, base_time=SIM_BASE_TIME,
            net_file=NET_FILE, batch_size=INITIAL_REQUESTS)
        print("[init] 预生成 %d 条乘客请求" % n)
    except Exception as e:
        print("[init] 乘客请求生成失败: %s" % e)

    # ---- 用 TraCI 启动 SUMO，以便逐步进入仿真循环 ----
    traci.start([sumoBinary,
                 '-c', 'data/manhattan.sumocfg',
                 '--duration-log.statistics',
                 '--start',
                 '--end', str(SIM_DURATION),
                 '--summary-output', SUMMARY_FILE,
                 '--tripinfo-output', TRIPINFO_FILE,
                 '--vehroute-output', VEHROUTES_FILE,
                 '--vehroute-output.exit-times', 'true',
                 '--vehroute-output.write-unfinished', 'true',
                 '--quit-on-end', 'true'])

    # 重置可视化状态（支持模块重载时多次运行）
    _active_persons.clear()
    _person_request_map.clear()
    _matched_fade_counter.clear()

    matched_total = 0     # 成功匹配条数累计
    failed_rounds = 0     # 抛出异常或回滚的匹配轮次数
    dropped_total = 0     # 成功下车人数累计
    dropoff_failed_rounds = 0  # 下车异常或回滚的轮次数
    known_vehicles = set()

    # 这条连接专门用于把 SUMO 新出现的车辆同步进 Vehicles 表
    sync_conn = get_connection(autocommit=True)
    sync_cur = sync_conn.cursor()

    # 乘客可视化专用数据库游标（只读查询，不影响业务事务）
    vis_conn = get_connection(autocommit=True)
    vis_cur = vis_conn.cursor()

    try:
        step = 0
        while step < SIM_DURATION:
            traci.simulationStep()

            # 把本步新进入路网的 SUMO 车辆登记进 Vehicles 表;
            # 否则 matcher 在 Vehicles 表里找不到可用车辆。
            new_rows = []
            for vid in traci.vehicle.getIDList():
                if vid in known_vehicles:
                    continue
                known_vehicles.add(vid)
                edge = traci.vehicle.getRoadID(vid) or 'unknown'
                new_rows.append((vid, edge, VEHICLE_CAPACITY))
            if new_rows:
                sync_cur.executemany(
                    "INSERT IGNORE INTO Vehicles "
                    "(vehicle_id, current_location, total_capacity, status) "
                    "VALUES (%s, %s, %s, 'available')",
                    new_rows)

            # 每 MATCH_INTERVAL 步执行一次拼车匹配; 异常被捕获但不影响仿真。
            if step % MATCH_INTERVAL == 0:
                try:
                    matched_total += match_pending_requests(
                        sim_time=step, base_time=SIM_BASE_TIME)
                except Exception as e:
                    failed_rounds += 1
                    print("[step %d] 匹配异常: %s" % (step, e))

            # 每 DROPOFF_INTERVAL 步处理一次乘客下车; 同样捕获异常,事务自身已回滚。
            if step % DROPOFF_INTERVAL == 0:
                try:
                    dropped_total += process_dropoffs(
                        sim_time=step, base_time=SIM_BASE_TIME)
                except Exception as e:
                    dropoff_failed_rounds += 1
                    print("[step %d] 下车异常: %s" % (step, e))

            # ---- 乘客可视化：每 PASSENGER_SYNC_INTERVAL 步同步一次 ----
            if step % PASSENGER_SYNC_INTERVAL == 0:
                try:
                    add_passenger_markers(vis_cur)
                    update_passenger_markers(vis_cur)
                except Exception as e:
                    print("[step %d] 乘客可视化异常: %s" % (step, e))

            # 每步都检查是否有需要移除的已匹配乘客标记
            remove_matched_passengers()

            step += 1
    finally:
        try:
            traci.close()
        except Exception:
            pass
        sync_cur.close()
        sync_conn.close()
        vis_cur.close()
        vis_conn.close()

    # ---- 仿真结束后打印匹配统计 ----
    _print_match_stats(matched_total, failed_rounds,
                       dropped_total, dropoff_failed_rounds)


def _print_match_stats(matched_total, failed_rounds,
                       dropped_total=0, dropoff_failed_rounds=0):
    """查询数据库, 汇总并打印本次拼车匹配的整体指标。"""
    try:
        conn = get_connection(autocommit=True)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM Trip_Requests WHERE status='matched'")
        matched_reqs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM Trip_Requests WHERE status='completed'")
        completed_reqs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM Trip_Requests WHERE status='pending'")
        pending_reqs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM Trip_Requests WHERE status IN ('rejected','timeout')")
        rejected_reqs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM Trip_Requests")
        total_reqs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM Vehicles")
        total_vehicles = cur.fetchone()[0]

        # 参与匹配车辆的平均载客率：用 Matches 表中实际匹配到的乘客数 / 车辆容量
        cur.execute(
            "SELECT IFNULL(AVG(load_ratio), 0) FROM ("
            "  SELECT m.vehicle_id, "
            "         COUNT(*) / v.total_capacity AS load_ratio "
            "  FROM Matches m "
            "  JOIN Vehicles v ON m.vehicle_id = v.vehicle_id "
            "  WHERE v.total_capacity > 0 "
            "  GROUP BY m.vehicle_id, v.total_capacity"
            ") sub")
        active_rate = float(cur.fetchone()[0] or 0)

        # 整车队平均载客率：所有匹配乘客数 / 全部车辆总容量
        cur.execute(
            "SELECT COUNT(*) FROM Matches")
        total_matched_passengers = cur.fetchone()[0]

        cur.execute(
            "SELECT IFNULL(SUM(total_capacity), 0) FROM Vehicles "
            "WHERE total_capacity > 0")
        total_fleet_capacity = int(cur.fetchone()[0] or 1)

        fleet_rate = total_matched_passengers / max(total_fleet_capacity, 1)

        cur.close()
        conn.close()
    except Exception as e:
        print("[stats] 统计查询失败: %s" % e)
        return

    print("=" * 50)
    print("           拼车匹配运行统计")
    print("=" * 50)
    print("  累计匹配成功次数       : %d" % matched_total)
    print("  匹配异常/回滚轮次     : %d" % failed_rounds)
    print("  累计下车成功人数       : %d" % dropped_total)
    print("  下车异常/回滚轮次     : %d" % dropoff_failed_rounds)
    print("  Trip_Requests 总数    : %d" % total_reqs)
    print("    -> 已匹配 (matched) : %d" % matched_reqs)
    print("    -> 已完成 (completed): %d" % completed_reqs)
    print("    -> 待匹配 (pending) : %d" % pending_reqs)
    print("    -> 已拒绝/超时      : %d" % rejected_reqs)
    print("  车辆总数              : %d" % total_vehicles)
    print("  参与匹配车辆平均载客率: %.2f%%" % (active_rate * 100))
    print("  整车队平均载客率      : %.2f%%" % (fleet_rate * 100))
    print("=" * 50)


def sim_to_ts(sim_seconds):
    return SIM_BASE_TIME + timedelta(seconds=float(sim_seconds))


# ---- 分批写入相关 ----------------------------------------------------------
BATCH_SIZE = 200


def _batch_executemany(conn, sql, rows, batch_size=BATCH_SIZE, label=""):
    """
    将 rows 按 batch_size(=200) 切片执行 executemany,每批独立 commit。
    - 遇到断线类错误自动 reconnect 重试最多 3 次;
    - 其它错误对当前批 rollback 并打印日志,跳过本批继续后面的批次,
      不让整个导入流程因为一批失败而中断。
    返回累计成功写入的行数。
    """
    if not rows:
        return 0
    inserted = 0
    total = len(rows)
    for start in range(0, total, batch_size):
        chunk = rows[start:start + batch_size]
        attempts = 0
        while True:
            try:
                ensure_alive(conn)
                cur = conn.cursor()
                try:
                    cur.executemany(sql, chunk)
                    conn.commit()
                finally:
                    cur.close()
                inserted += len(chunk)
                break
            except Error as e:
                attempts += 1
                if is_disconnect_error(e) and attempts <= 3:
                    print("[batch %s] 连接断开 (errno=%s),重试 %d/3"
                          % (label, getattr(e, 'errno', None), attempts))
                    try:
                        conn.reconnect(attempts=3, delay=1)
                    except Error as re:
                        print("[batch %s] 重连失败: %s" % (label, re))
                    continue
                try:
                    conn.rollback()
                except Error:
                    pass
                print("[batch %s] 批次 [%d-%d] 写入失败: %s"
                      % (label, start, start + len(chunk), e))
                break
    return inserted


def import_performance_metrics(conn):
    if not os.path.exists(SUMMARY_FILE):
        print("warning: %s missing, skip Performance_Metrics" % SUMMARY_FILE)
        return
    metric_keys = ['running', 'meanSpeed', 'meanWaitingTime',
                   'meanTravelTime', 'halting', 'inserted', 'ended']
    rows = []
    for step in ET.parse(SUMMARY_FILE).getroot().findall('step'):
        t = float(step.get('time'))
        ws = sim_to_ts(t)
        we = sim_to_ts(t + 1)
        for key in metric_keys:
            v = step.get(key)
            if v is None:
                continue
            rows.append((key, float(v), ws, we))
    if not rows:
        return
    inserted = _batch_executemany(
        conn,
        "INSERT INTO Performance_Metrics "
        "(metric_name, metric_value, window_start, window_end) "
        "VALUES (%s, %s, %s, %s)",
        rows, label="Performance_Metrics")
    print("inserted %d Performance_Metrics rows" % inserted)


def import_route_stops(conn):
    if not os.path.exists(VEHROUTES_FILE):
        print("warning: %s missing, skip Route_Stops" % VEHROUTES_FILE)
        return
    vehicle_rows = []
    stop_rows = []
    for veh in ET.parse(VEHROUTES_FILE).getroot().findall('vehicle'):
        vid = veh.get('id')
        route = veh.find('route')
        if route is None:
            continue
        edges = route.get('edges', '').split()
        exit_times = route.get('exitTimes', '').split()
        if not edges:
            continue
        depart = float(veh.get('depart', '0'))
        vehicle_rows.append((vid, edges[-1]))
        n = len(edges)
        for i, edge in enumerate(edges):
            if i == 0:
                stop_type = 'pickup'
            elif i == n - 1:
                stop_type = 'dropoff'
            else:
                stop_type = None
            if i < len(exit_times):
                actual_time = sim_to_ts(exit_times[i])
                completed = True
            else:
                actual_time = None
                completed = False
            stop_rows.append((vid, edge, stop_type, sim_to_ts(depart),
                              actual_time, completed, i + 1))

    # Vehicles row must exist before Route_Stops because of the FK.
    inserted_v = _batch_executemany(
        conn,
        "INSERT IGNORE INTO Vehicles "
        "(vehicle_id, current_location, total_capacity) "
        "VALUES (%s, %s, 4)",
        vehicle_rows, label="Vehicles")
    print("ensured %d Vehicles rows" % inserted_v)

    inserted_s = _batch_executemany(
        conn,
        "INSERT INTO Route_Stops "
        "(vehicle_id, location, stop_type, scheduled_time, actual_time, "
        "completed, stop_sequence) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        stop_rows, label="Route_Stops")
    print("inserted %d Route_Stops rows" % inserted_s)


def import_results_to_db():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
    except Error as e:
        sys.exit("cannot connect to MySQL: %s" % e)
    try:
        # 清理动作单独事务,提前 commit,避免和后续大批量插入挤在一个事务里。
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "DELETE FROM Route_Stops WHERE passenger_id IS NULL")
                cursor.execute("TRUNCATE TABLE Performance_Metrics")
                conn.commit()
            finally:
                cursor.close()
        except Error as e:
            try:
                conn.rollback()
            except Error:
                pass
            sys.exit("database cleanup failed: %s" % e)

        # 分批导入:函数内部已经按 200 行/批 commit 并处理断线重连。
        try:
            import_performance_metrics(conn)
        except Error as e:
            print("[import] Performance_Metrics 导入失败: %s" % e)
        try:
            import_route_stops(conn)
        except Error as e:
            print("[import] Route_Stops 导入失败: %s" % e)
    finally:
        try:
            conn.close()
        except Error:
            pass
    print("database import completed")


if __name__ == '__main__':
    run_simulation()
    import_results_to_db()
