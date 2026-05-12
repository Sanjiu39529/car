"""
Transactional Integrity in Shared-Ride Matching
Core matching logic with capacity enforcement and transaction safety
"""

import sqlite3
import uuid
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import threading
from contextlib import contextmanager
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RideMatchingSystem:
    def __init__(self, db_path: str = "shared_rides.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._initialize_db()

    def _initialize_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # Check if tables exist
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Vehicles'")
            if cursor.fetchone() is None:
                # Create tables
                with open('schema.sql', 'r') as f:
                    conn.executescript(f.read())
                conn.commit()
                logger.info("Database initialized successfully")
            else:
                logger.info("Database already exists")

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path, isolation_level='DEFERRED')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        try:
            yield conn
        finally:
            conn.close()

    def add_vehicle(self, vehicle_id: str, location: str, capacity: int = 4) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO Vehicles (vehicle_id, current_location, total_capacity, current_load, status) "
                    "VALUES (?, ?, ?, 0, 'available')",
                    (vehicle_id, location, capacity)
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                logger.warning(f"Vehicle {vehicle_id} already exists")
                return False

    def add_trip_request(self, passenger_id: str, origin: str, destination: str,
                         pickup_window_start: datetime, pickup_window_end: datetime,
                         max_extra_stops: int = 1) -> str:
        request_id = str(uuid.uuid4())
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO Trip_Requests (request_id, passenger_id, origin, destination, "
                    "pickup_window_start, pickup_window_end, max_extra_stops, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (request_id, passenger_id, origin, destination,
                     pickup_window_start, pickup_window_end, max_extra_stops)
                )
                conn.commit()
                logger.info(f"Trip request {request_id} added for passenger {passenger_id}")
                return request_id
            except Exception as e:
                logger.error(f"Failed to add trip request: {e}")
                raise

    def find_overlapping_requests(self, time_window_seconds: int = 600) -> List[Dict]:
        """
        Find passengers with overlapping origin-destination paths within time window.
        Uses sophisticated matching considering origin, destination, and timing.
        """
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = """
            SELECT
                r1.request_id as request_id_1,
                r1.origin as origin_1,
                r1.destination as destination_1,
                r1.pickup_window_start as window_start_1,
                r1.pickup_window_end as window_end_1,
                r1.max_extra_stops as max_extra_stops_1,
                r2.request_id as request_id_2,
                r2.origin as origin_2,
                r2.destination as destination_2,
                r2.pickup_window_start as window_start_2,
                r2.pickup_window_end as window_end_2,
                r2.max_extra_stops as max_extra_stops_2,
                ABS(julianday(r1.pickup_window_end) - julianday(r2.pickup_window_start)) * 86400 as time_diff
            FROM Trip_Requests r1
            JOIN Trip_Requests r2 ON r1.request_id != r2.request_id
            WHERE r1.status = 'pending'
              AND r2.status = 'pending'
              AND r1.pickup_window_start <= ?
              AND (
                  -- Same origin (pickup sharing)
                  r1.origin = r2.origin
                  OR
                  -- Same destination (dropoff sharing)
                  r1.destination = r2.destination
                  OR
                  -- Overlapping route (one's origin near other's destination)
                  ABS(julianday(r1.pickup_window_end) - julianday(r2.pickup_window_start)) * 86400 <= ?
              )
              AND r1.max_extra_stops > 0
              AND r2.max_extra_stops > 0
            ORDER BY time_diff ASC
            """

            end_time = datetime.now() + timedelta(seconds=time_window_seconds)
            cursor.execute(query, (end_time, time_window_seconds))

            results = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Found {len(results)} overlapping request pairs")
            return results

    def find_available_vehicles(self, current_location: str = None) -> List[Dict]:
        """Find vehicles with available capacity."""
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if current_location:
                query = """
                SELECT * FROM Available_Vehicles
                WHERE available_seats > 0
                ORDER BY current_location = ? DESC, available_seats DESC
                """
                cursor.execute(query, (current_location,))
            else:
                query = """
                SELECT * FROM Available_Vehicles
                WHERE available_seats > 0
                ORDER BY available_seats DESC
                """
                cursor.execute(query)

            return [dict(row) for row in cursor.fetchall()]

    def attempt_match_with_transaction(self, request_id: str,
                                       max_retries: int = 3,
                                       timeout_ms: int = 5000) -> Tuple[bool, Optional[Dict]]:
        """
        Attempt to match a trip request with an available vehicle using transaction.
        Enforces capacity constraint: current_load + new_request <= capacity
        Returns (success, match_info) tuple
        """
        transaction_id = str(uuid.uuid4())
        attempt = 0

        while attempt < max_retries:
            attempt += 1
            try:
                with self.get_connection() as conn:
                    conn.row_factory = sqlite3.Row

                    # Set transaction timeout
                    cursor = conn.cursor()
                    cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")

                    # Begin transaction with explicit isolation
                    cursor.execute("BEGIN IMMEDIATE")

                    try:
                        # Get request details
                        cursor.execute(
                            "SELECT * FROM Trip_Requests WHERE request_id = ? AND status = 'pending'",
                            (request_id,)
                        )
                        request = cursor.fetchone()

                        if not request:
                            conn.rollback()
                            return False, {"error": "Request not found or already processed"}

                        request = dict(request)

                        # Find best matching vehicle
                        query = """
                        SELECT
                            v.vehicle_id,
                            v.total_capacity,
                            v.current_load,
                            (v.total_capacity - v.current_load) as available_seats,
                            v.current_location,
                            COUNT(rs.stop_id) as current_stops
                        FROM Vehicles v
                        LEFT JOIN Route_Stops rs ON v.vehicle_id = rs.vehicle_id AND NOT rs.completed
                        WHERE v.status = 'available'
                          AND v.current_load < v.total_capacity
                          AND (v.current_load + 1) <= v.total_capacity
                        GROUP BY v.vehicle_id
                        ORDER BY
                            CASE WHEN v.current_location = ? THEN 0 ELSE 1 END,
                            current_stops ASC,
                            (v.total_capacity - v.current_load) DESC
                        LIMIT 1
                        """

                        cursor.execute(query, (request['origin'],))
                        vehicle = cursor.fetchone()

                        if not vehicle:
                            conn.rollback()
                            return False, {"error": "No available vehicle"}

                        vehicle = dict(vehicle)

                        # Check capacity constraint explicitly
                        if vehicle['current_load'] + 1 > vehicle['total_capacity']:
                            conn.rollback()
                            return False, {"error": "Capacity constraint violated"}

                        # Update request status
                        now = datetime.now()
                        estimated_pickup = now + timedelta(minutes=2)
                        estimated_dropoff = estimated_pickup + timedelta(minutes=15)

                        cursor.execute(
                            "UPDATE Trip_Requests SET status = 'matched' WHERE request_id = ?",
                            (request_id,)
                        )

                        # Create match record
                        match_id = str(uuid.uuid4())
                        cursor.execute(
                            "INSERT INTO Matches (match_id, vehicle_id, request_id, "
                            "estimated_pickup_time, estimated_dropoff_time, shared) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (match_id, vehicle['vehicle_id'], request_id,
                             estimated_pickup, estimated_dropoff, vehicle['current_load'] > 0)
                        )

                        # Add passenger to Current_Passengers
                        cursor.execute(
                            "INSERT INTO Current_Passengers (passenger_id, vehicle_id, request_id, "
                            "pickup_time, pickup_location, destination, is_en_route) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (request['passenger_id'], vehicle['vehicle_id'], request_id,
                             estimated_pickup, request['origin'], request['destination'], False)
                        )

                        # Update vehicle status if full
                        if vehicle['current_load'] + 1 >= vehicle['total_capacity']:
                            cursor.execute(
                                "UPDATE Vehicles SET status = 'busy' WHERE vehicle_id = ?",
                                (vehicle['vehicle_id'],)
                            )

                        # Commit transaction
                        conn.commit()

                        match_info = {
                            "match_id": match_id,
                            "vehicle_id": vehicle['vehicle_id'],
                            "request_id": request_id,
                            "pickup_time": estimated_pickup,
                            "dropoff_time": estimated_dropoff,
                            "shared": vehicle['current_load'] > 0,
                            "transaction_id": transaction_id
                        }

                        logger.info(f"Successfully matched request {request_id} to vehicle {vehicle['vehicle_id']}")
                        return True, match_info

                    except sqlite3.Error as e:
                        conn.rollback()
                        if "database is locked" in str(e) or "timeout" in str(e):
                            logger.warning(f"Transaction timeout on attempt {attempt}, retrying...")
                            time.sleep(0.1 * attempt)
                            continue
                        raise

            except Exception as e:
                logger.error(f"Error during matching: {e}")
                if attempt >= max_retries:
                    return False, {"error": f"Transaction failed after {max_retries} retries: {str(e)}"}

        return False, {"error": "Max retries exceeded"}

    def get_vehicle_load(self, vehicle_id: str) -> Dict:
        """Get current load information for a vehicle."""
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    v.vehicle_id,
                    v.total_capacity,
                    v.current_load,
                    (v.total_capacity - v.current_load) as available_seats,
                    v.status,
                    COUNT(cp.passenger_id) as passenger_count
                FROM Vehicles v
                LEFT JOIN Current_Passengers cp ON v.vehicle_id = cp.vehicle_id AND cp.is_en_route = TRUE
                WHERE v.vehicle_id = ?
                GROUP BY v.vehicle_id
            """, (vehicle_id,))

            result = cursor.fetchone()
            return dict(result) if result else {}

    def complete_trip(self, request_id: str, vehicle_id: str) -> bool:
        """Mark a trip as completed and update vehicle load."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")

                # Update passenger status
                cursor.execute(
                    "UPDATE Current_Passengers SET is_en_route = FALSE, actual_dropoff_time = ? "
                    "WHERE request_id = ? AND vehicle_id = ?",
                    (datetime.now(), request_id, vehicle_id)
                )

                # Update trip request status
                cursor.execute(
                    "UPDATE Trip_Requests SET status = 'completed' WHERE request_id = ?",
                    (request_id,)
                )

                # Check if vehicle should become available
                cursor.execute(
                    "SELECT COUNT(*) FROM Current_Passengers WHERE vehicle_id = ? AND is_en_route = TRUE",
                    (vehicle_id,)
                )
                en_route_count = cursor.fetchone()[0]

                cursor.execute(
                    "SELECT total_capacity FROM Vehicles WHERE vehicle_id = ?",
                    (vehicle_id,)
                )
                capacity = cursor.fetchone()[0]

                if en_route_count < capacity:
                    cursor.execute(
                        "UPDATE Vehicles SET status = 'available' WHERE vehicle_id = ?",
                        (vehicle_id,)
                    )

                conn.commit()
                return True

        except Exception as e:
            logger.error(f"Failed to complete trip: {e}")
            return False

    def get_system_metrics(self) -> Dict:
        """Get current system performance metrics."""
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Average Vehicle Occupancy (AVO)
            cursor.execute("""
                SELECT
                    AVG(CAST(current_load AS REAL) / total_capacity) as avg_occupancy
                FROM Vehicles
                WHERE total_capacity > 0
            """)
            avg_occupancy = cursor.fetchone()['avg_occupancy'] or 0.0

            # Request statistics
            cursor.execute("""
                SELECT
                    COUNT(*) as total_requests,
                    SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) as matched_requests,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_requests,
                    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected_requests,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_requests
                FROM Trip_Requests
            """)
            req_stats = dict(cursor.fetchone())

            # Vehicle statistics
            cursor.execute("""
                SELECT
                    COUNT(*) as total_vehicles,
                    SUM(current_load) as total_passengers,
                    SUM(total_capacity) as total_capacity,
                    SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) as available_vehicles
                FROM Vehicles
            """)
            vehicle_stats = dict(cursor.fetchone())

            # Shared rides count
            cursor.execute("""
                SELECT COUNT(*) as shared_rides
                FROM Matches
                WHERE shared = TRUE
            """)
            shared_rides = cursor.fetchone()['shared_rides']

            return {
                "average_vehicle_occupancy": avg_occupancy,
                "total_requests": req_stats['total_requests'] or 0,
                "matched_requests": req_stats['matched_requests'] or 0,
                "completed_requests": req_stats['completed_requests'] or 0,
                "rejected_requests": req_stats['rejected_requests'] or 0,
                "timeout_requests": req_stats['timeout_requests'] or 0,
                "match_rate": (req_stats['matched_requests'] / req_stats['total_requests'] * 100
                              if req_stats['total_requests'] else 0),
                "completion_rate": (req_stats['completed_requests'] / req_stats['matched_requests'] * 100
                                   if req_stats['matched_requests'] else 0),
                "rejection_rate": (req_stats['rejected_requests'] / req_stats['total_requests'] * 100
                                  if req_stats['total_requests'] else 0),
                "timeout_rate": (req_stats['timeout_requests'] / req_stats['total_requests'] * 100
                                if req_stats['total_requests'] else 0),
                "total_vehicles": vehicle_stats['total_vehicles'] or 0,
                "available_vehicles": vehicle_stats['available_vehicles'] or 0,
                "total_passengers": vehicle_stats['total_passengers'] or 0,
                "total_capacity": vehicle_stats['total_capacity'] or 0,
                "shared_rides": shared_rides
            }
