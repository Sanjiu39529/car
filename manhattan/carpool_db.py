# -*- coding: utf-8 -*-
# 统一封装拼车仿真项目的 MySQL 连接配置与连接获取入口。
# 其它模块通过 from carpool_db import get_connection 调用，
# 避免在多个文件里散布数据库账号密码。

import time
import mysql.connector
from mysql.connector import Error

DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'root',
    'password': '123456',
    'database': 'carpool',
    # 让 datetime 直接以 Python 对象传入/取出
    'use_pure': True,
    # 建连超时,避免网络异常时一直挂死;读写阶段的超时由 wait_timeout 管。
    'connection_timeout': 30,
}

# MySQL / mysql-connector 常见的"连接已断"错误码:
#   2006 MySQL server has gone away
#   2013 Lost connection to MySQL server during query
#   2055 Lost connection to MySQL server at '%s', system error: %d
#   4031 The client was disconnected by the server because of inactivity
DISCONNECT_ERRNOS = (2006, 2013, 2055, 4031)


def is_disconnect_error(err):
    """判断异常是否为 MySQL 断连类错误。"""
    return getattr(err, 'errno', None) in DISCONNECT_ERRNOS


def get_connection(autocommit=True, retries=3, delay=1):
    """
    获取一个新的 MySQL 连接;事务场景请传 autocommit=False。

    - 建连失败属于断连/超时类错误时,自动重试最多 retries 次,
      缓解 MySQL 短暂网络抖动或重启;
    - 建连成功后会主动 ping(reconnect=True),让连接在交还调用方
      之前确实可用,避免拿到一个"刚断开"的句柄。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            conn = mysql.connector.connect(**DB_CONFIG)
            conn.autocommit = autocommit
            # 主动 ping 一次,确保返回的是真正活着的连接。
            conn.ping(reconnect=True, attempts=retries, delay=delay)
            return conn
        except Error as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(delay)
    # 最终仍失败:把最后一个异常抛出,交由调用方处理。
    raise last_err


def ensure_alive(conn, attempts=3, delay=1):
    """
    确认 MySQL 连接存活;断开则尝试 ping/reconnect。

    在长时间运行的循环(SUMO 主循环、大批量导入)里,在使用 cursor 之前
    调用一次,可避免 wait_timeout / 网络抖动 引发的
    'MySQL server has gone away' 错误。
    """
    try:
        conn.ping(reconnect=True, attempts=attempts, delay=delay)
    except Error:
        # ping 自身失败时再显式 reconnect 一次,失败由调用方捕获。
        conn.reconnect(attempts=attempts, delay=delay)
