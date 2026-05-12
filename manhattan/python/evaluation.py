"""
Performance Evaluation for Shared-Ride Matching System
Calculates AVO, VMT reduction, and other key metrics
"""

import sqlite3
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# Optional dependencies
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# Define fallback functions
def mean(values):
    if HAS_NUMPY:
        return mean(values)
    return sum(values) / len(values) if values else 0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PerformanceEvaluator:
    def __init__(self, db_path: str = "shared_rides.db"):
        self.db_path = db_path
        self.metrics_history = []

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def calculate_avo(self, window_start: Optional[datetime] = None,
                     window_end: Optional[datetime] = None) -> float:
        """
        Calculate Average Vehicle Occupancy (AVO).
        AVO = Average(current_load / total_capacity) across all vehicles.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            query = """
            SELECT
                AVG(CAST(current_load AS REAL) / total_capacity) as avg_occupancy
            FROM Vehicles
            WHERE total_capacity > 0
            """

            if window_start and window_end:
                # Use historical data if timestamps are available
                pass

            cursor.execute(query)
            result = cursor.fetchone()
            return result['avg_occupancy'] if result and result['avg_occupancy'] else 0.0

    def calculate_vmt_metrics(self, baseline_requests: int = 3000) -> Dict[str, float]:
        """
        Calculate Vehicle Miles Traveled metrics.
        Compares shared rides vs. individual rides baseline.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get shared ride count
            cursor.execute("SELECT COUNT(*) as count FROM Matches WHERE shared = TRUE")
            shared_count = cursor.fetchone()['count'] or 0

            # Total rides
            cursor.execute("SELECT COUNT(*) as count FROM Trip_Requests WHERE status IN ('matched', 'completed')")
            total_rides = cursor.fetchone()['count'] or 0

            # Vehicle utilization
            cursor.execute("""
                SELECT
                    COUNT(*) as active_vehicles,
                    SUM(current_load) as total_passengers,
                    AVG(CAST(current_load AS REAL) / total_capacity) as avg_occupancy
                FROM Vehicles
                WHERE current_load > 0
            """)
            vehicle_stats = cursor.fetchone()

            # VMT reduction calculation
            # Assumptions:
            # - Each individual ride would require a dedicated vehicle
            # - Shared rides reduce total vehicle miles
            # - Baseline: each request uses a full vehicle trip
            # - Shared: multiple requests share vehicle trips

            if total_rides > 0 and vehicle_stats['total_passengers'] > 0:
                sharing_ratio = shared_count / total_rides
                avg_occupancy = vehicle_stats['avg_occupancy'] or 1.0

                # VMT reduction due to sharing
                # Higher occupancy means fewer vehicle trips needed
                vmt_reduction = (1 - (1 / avg_occupancy)) * sharing_ratio * 100
            else:
                vmt_reduction = 0.0

            return {
                "total_rides": total_rides,
                "shared_rides": shared_count,
                "sharing_ratio": sharing_ratio if total_rides > 0 else 0.0,
                "active_vehicles": vehicle_stats['active_vehicles'] or 0,
                "avg_occupancy": vehicle_stats['avg_occupancy'] or 0.0,
                "vmt_reduction_percent": vmt_reduction
            }

    def calculate_rejection_rate(self, window_start: Optional[datetime] = None,
                                 window_end: Optional[datetime] = None) -> Dict[str, float]:
        """
        Calculate rejection rates, including those caused by transaction timeouts.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            base_query = """
            SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) as matched,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected,
                SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
            FROM Trip_Requests
            """

            if window_start and window_end:
                base_query += f" WHERE created_at BETWEEN '{window_start}' AND '{window_end}'"

            cursor.execute(base_query)
            result = cursor.fetchone()

            total = result['total_requests'] or 0

            if total > 0:
                return {
                    "total_requests": total,
                    "matched_rate": (result['matched'] or 0) / total * 100,
                    "completion_rate": (result['completed'] or 0) / total * 100,
                    "rejection_rate": (result['rejected'] or 0) / total * 100,
                    "timeout_rate": (result['timeout'] or 0) / total * 100,
                    "pending_rate": (result['pending'] or 0) / total * 100
                }
            else:
                return {
                    "total_requests": 0,
                    "matched_rate": 0.0,
                    "completion_rate": 0.0,
                    "rejection_rate": 0.0,
                    "timeout_rate": 0.0,
                    "pending_rate": 0.0
                }

    def calculate_transaction_metrics(self) -> Dict[str, float]:
        """
        Calculate transaction-related metrics.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Count transactions by type
            cursor.execute("""
                SELECT
                    operation,
                    COUNT(*) as count
                FROM Transaction_Log
                GROUP BY operation
            """)
            operations = {row['operation']: row['count'] for row in cursor.fetchall()}

            # Transaction failures (from transaction log)
            cursor.execute("""
                SELECT COUNT(*) as count
                FROM Transaction_Log
                WHERE operation LIKE '%FAIL%' OR operation LIKE '%ROLLBACK%'
            """)
            failures = cursor.fetchone()['count'] or 0

            return {
                "total_transactions": sum(operations.values()),
                "successful_transactions": operations.get('UPDATE', 0) + operations.get('INSERT', 0),
                "failed_transactions": failures,
                "transaction_types": operations
            }

    def get_peak_matching_periods(self) -> List[Dict]:
        """
        Identify periods with high matching activity for performance analysis.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    DATE_TRUNC('hour', matched_at) as hour,
                    COUNT(*) as match_count
                FROM Matches
                GROUP BY hour
                ORDER BY match_count DESC
                LIMIT 10
            """)

            return [{"hour": row['hour'], "count": row['match_count']} for row in cursor.fetchall()]

    def calculate_wait_time_metrics(self) -> Dict[str, float]:
        """
        Calculate passenger wait times.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Calculate average time between request and match
            cursor.execute("""
                SELECT
                    AVG(JULIANDAY(m.matched_at) - JULIANDAY(r.created_at)) * 86400 as avg_match_time_seconds,
                    MIN(JULIANDAY(m.matched_at) - JULIANDAY(r.created_at)) * 86400 as min_match_time_seconds,
                    MAX(JULIANDAY(m.matched_at) - JULIANDAY(r.created_at)) * 86400 as max_match_time_seconds
                FROM Matches m
                JOIN Trip_Requests r ON m.request_id = r.request_id
            """)

            result = cursor.fetchone()

            return {
                "avg_wait_time_seconds": result['avg_match_time_seconds'] or 0.0,
                "min_wait_time_seconds": result['min_match_time_seconds'] or 0.0,
                "max_wait_time_seconds": result['max_match_time_seconds'] or 0.0
            }

    def generate_full_report(self, save_path: str = None) -> Dict:
        """
        Generate a comprehensive performance report.
        """
        logger.info("Generating performance report...")

        report = {
            "timestamp": datetime.now().isoformat(),
            "metrics": {
                "average_vehicle_occupancy": self.calculate_avo(),
                "vmt": self.calculate_vmt_metrics(),
                "rejection_rates": self.calculate_rejection_rate(),
                "transaction_metrics": self.calculate_transaction_metrics(),
                "wait_times": self.calculate_wait_time_metrics()
            },
            "scale_info": {
                "network_size": "5km x 5km",
                "vehicles": 500,
                "requests": 3000,
                "seats_per_vehicle": 4,
                "simulation_window": "7200 seconds (2 hours)"
            }
        }

        self.metrics_history.append(report)

        if save_path:
            self.save_report(report, save_path)
            logger.info(f"Report saved to {save_path}")

        return report

    def save_report(self, report: Dict, path: str):
        """Save report to file."""
        import json
        with open(path, 'w') as f:
            json.dump(report, f, indent=2, default=str)

    def plot_metrics(self, metrics: Dict = None, output_path: str = "metrics_plot.png"):
        """
        Visualize key metrics.
        """
        if metrics is None:
            metrics = self.generate_full_report()["metrics"]

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle('Shared-Ride Matching Performance Metrics', fontsize=16, fontweight='bold')

        # 1. Average Vehicle Occupancy
        axes[0, 0].bar(['AVO'], [metrics["average_vehicle_occupancy"] * 100],
                      color='#2ecc71', alpha=0.8)
        axes[0, 0].set_ylabel('Occupancy (%)')
        axes[0, 0].set_title(f'Average Vehicle Occupancy: {metrics["average_vehicle_occupancy"]*100:.1f}%')
        axes[0, 0].set_ylim([0, 100])
        axes[0, 0].grid(axis='y', alpha=0.3)

        # 2. VMT Reduction
        vmt = metrics["vmt"]
        axes[0, 1].bar(['VMT Reduction'], [vmt["vmt_reduction_percent"]],
                      color='#3498db', alpha=0.8)
        axes[0, 1].set_ylabel('Reduction (%)')
        axes[0, 1].set_title(f'Vehicle Miles Traveled Reduction: {vmt["vmt_reduction_percent"]:.1f}%')
        axes[0, 1].set_ylim([0, 100])
        axes[0, 1].grid(axis='y', alpha=0.3)

        # 3. Request Status Breakdown
        rr = metrics["rejection_rates"]
        categories = ['Matched', 'Completed', 'Rejected', 'Timeout', 'Pending']
        values = [
            rr["matched_rate"],
            rr["completion_rate"],
            rr["rejection_rate"],
            rr["timeout_rate"],
            rr["pending_rate"]
        ]
        colors = ['#27ae60', '#2ecc71', '#e74c3c', '#f39c12', '#95a5a6']

        axes[1, 0].bar(categories, values, color=colors, alpha=0.8)
        axes[1, 0].set_ylabel('Rate (%)')
        axes[1, 0].set_title('Request Status Distribution')
        axes[1, 0].tick_params(axis='x', rotation=45)
        axes[1, 0].grid(axis='y', alpha=0.3)

        # 4. Transaction Metrics
        tm = metrics["transaction_metrics"]
        trans_labels = ['Successful', 'Failed']
        trans_values = [tm["successful_transactions"], tm["failed_transactions"]]
        trans_colors = ['#3498db', '#e74c3c']

        axes[1, 1].bar(trans_labels, trans_values, color=trans_colors, alpha=0.8)
        axes[1, 1].set_ylabel('Count')
        axes[1, 1].set_title('Transaction Outcomes')
        axes[1, 1].grid(axis='y', alpha=0.3)

        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        logger.info(f"Metrics plot saved to {output_path}")

        return fig

    def compare_scenarios(self, baseline_data: Dict, shared_data: Dict) -> Dict:
        """
        Compare baseline (no sharing) vs shared ride scenarios.
        """
        comparison = {
            "baseline": baseline_data,
            "shared": shared_data,
            "improvements": {
                "avo_improvement": (shared_data["average_vehicle_occupancy"] -
                                  baseline_data.get("average_vehicle_occupancy", 0.25)),
                "vmt_savings": shared_data["vmt"]["vmt_reduction_percent"],
                "vehicles_needed_baseline": baseline_data["total_requests"],
                "vehicles_needed_shared": int(baseline_data["total_requests"] *
                                              (1 - shared_data["vmt"]["vmt_reduction_percent"] / 100))
            }
        }

        return comparison


class Benchmark:
    """
    Benchmark utility for stress testing the matching system.
    """

    def __init__(self, matching_system, evaluator):
        self.ms = matching_system
        self.evaluator = evaluator
        self.results = []

    def run_stress_test(self, num_requests: int, batch_size: int = 10) -> Dict:
        """
        Run a stress test with many concurrent requests.
        Tests transaction integrity under load.
        """
        logger.info(f"Starting stress test with {num_requests} requests, batch size {batch_size}")

        start_time = time.time()
        timeouts = 0
        matches = 0
        rejections = 0

        from concurrent.futures import ThreadPoolExecutor

        def process_request(i):
            nonlocal timeouts, matches, rejections

            origin = f"edge_{i % 100}"
            dest = f"edge_{(i + 50) % 100}"
            now = datetime.now()

            request_id = self.ms.add_trip_request(
                passenger_id=f"passenger_{i}",
                origin=origin,
                destination=dest,
                pickup_window_start=now,
                pickup_window_end=now + timedelta(minutes=10)
            )

            success, result = self.ms.attempt_match_with_transaction(
                request_id,
                max_retries=3,
                timeout_ms=1000
            )

            if success:
                matches += 1
            else:
                if "timeout" in str(result.get("error", "")).lower():
                    timeouts += 1
                else:
                    rejections += 1

            return i, success

        # Process requests in batches
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            for i in range(0, num_requests, batch_size):
                batch_end = min(i + batch_size, num_requests)
                list(executor.map(process_request, range(i, batch_end)))

        elapsed = time.time() - start_time

        result = {
            "total_requests": num_requests,
            "successful_matches": matches,
            "timeouts": timeouts,
            "rejections": rejections,
            "elapsed_time_seconds": elapsed,
            "requests_per_second": num_requests / elapsed,
            "timeout_rate": (timeouts / num_requests * 100) if num_requests > 0 else 0,
            "match_rate": (matches / num_requests * 100) if num_requests > 0 else 0
        }

        self.results.append(result)
        return result

    def run_peak_load_test(self) -> Dict:
        """
        Simulate peak load scenario (7:30 AM - 8:30 AM rush hour).
        """
        logger.info("Running peak load test...")

        num_requests = 1500  # 75% of total requests in peak hour
        return self.run_stress_test(num_requests, batch_size=50)
