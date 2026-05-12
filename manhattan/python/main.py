"""
Shared-Ride Matching System - Main Entry Point
Transactional Integrity in Shared-Ride Matching
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from typing import List

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ride_matching import RideMatchingSystem
from traci_integration import SUMOIntegration
from evaluation import PerformanceEvaluator, Benchmark

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SharedRideSimulation:
    def __init__(self, sumo_gui: bool = False, db_path: str = "shared_rides.db"):
        # Configuration
        self.config = {
            "network_size": "5km x 5km",
            "num_vehicles": 500,
            "num_requests": 3000,
            "seats_per_vehicle": 4,
            "simulation_time": 7200,  # 2 hours in seconds
            "time_step": 1.0,  # seconds
        }

        # Initialize components
        self.db_path = db_path
        self.matching_system = RideMatchingSystem(db_path)
        self.evaluator = PerformanceEvaluator(db_path)

        # SUMO integration
        self.sumo_gui = sumo_gui
        self.sumo = None

        logger.info("Shared-Ride Matching System initialized")

    def initialize_vehicles(self):
        """Initialize vehicles in the database."""
        logger.info(f"Initializing {self.config['num_vehicles']} vehicles...")

        for i in range(self.config['num_vehicles']):
            vehicle_id = f"vehicle_{i:04d}"
            location = f"edge_{i % 100}"
            self.matching_system.add_vehicle(
                vehicle_id=vehicle_id,
                location=location,
                capacity=self.config['seats_per_vehicle']
            )

        logger.info("Vehicles initialized successfully")

    def initialize_sumo(self):
        """Initialize SUMO simulation."""
        sumo_binary = "sumo-gui" if self.sumo_gui else "sumo"

        # Check if SUMO is available
        try:
            import whichcraft
        except ImportError:
            import shutil as whichcraft

        sumo_cmd = [
            sumo_binary,
            "-c", "../data/manhattan.sumocfg",
            "--start", "--quit-on-end",
            "--remote-port", "8813"
        ]

        self.sumo = SUMOIntegration(sumo_cmd)
        logger.info("SUMO integration ready (will start on first step)")

    def generate_requests(self) -> List[str]:
        """Generate trip requests for the simulation."""
        logger.info(f"Generating {self.config['num_requests']} trip requests...")

        request_ids = []
        now = datetime.now()

        # Distribute requests over simulation time
        for i in range(self.config['num_requests']):
            # Simulate realistic arrival pattern
            time_offset = (i / self.config['num_requests']) * self.config['simulation_time']

            # Peak hours: more requests around 30% and 70% of simulation time
            import random
            if random.random() < 0.3:  # 30% chance of peak hour request
                peak_offset = random.choice([self.config['simulation_time'] * 0.3,
                                           self.config['simulation_time'] * 0.7])
                time_offset = peak_offset + random.uniform(-600, 600)

            request_time = now + timedelta(seconds=time_offset)
            window_start = request_time
            window_end = request_time + timedelta(minutes=10)

            request_id = self.matching_system.add_trip_request(
                passenger_id=f"passenger_{i:04d}",
                origin=f"edge_{random.randint(0, 99)}",
                destination=f"edge_{random.randint(100, 199)}",
                pickup_window_start=window_start,
                pickup_window_end=window_end,
                max_extra_stops=random.randint(0, 2)
            )

            request_ids.append(request_id)

        logger.info("Trip requests generated")
        return request_ids

    def process_pending_requests(self, current_time: datetime):
        """Process pending trip requests."""
        try:
            # Get pending requests
            with self.matching_system.get_connection() as conn:
                import sqlite3
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT request_id
                    FROM Trip_Requests
                    WHERE status = 'pending'
                      AND pickup_window_start <= ?
                    ORDER BY created_at ASC
                    LIMIT 50
                """, (current_time,))

                pending_requests = [row['request_id'] for row in cursor.fetchall()]

            # Attempt to match each request
            matched_count = 0
            for request_id in pending_requests:
                success, result = self.matching_system.attempt_match_with_transaction(
                    request_id,
                    max_retries=3,
                    timeout_ms=2000
                )

                if success:
                    matched_count += 1

                    # If SUMO is running, update vehicle routes
                    if self.sumo and self.sumo.connected:
                        vehicle_id = result['vehicle_id']
                        # Reroute vehicle to pickup location
                        # This would use real edge IDs in production
                        pass

            return matched_count

        except Exception as e:
            logger.error(f"Error processing requests: {e}")
            return 0

    def complete_rides(self, current_time: datetime):
        """Mark completed rides."""
        try:
            with self.matching_system.get_connection() as conn:
                import sqlite3
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Find matched rides that should be completed
                cursor.execute("""
                    SELECT m.request_id, m.vehicle_id, m.estimated_dropoff_time
                    FROM Matches m
                    JOIN Trip_Requests r ON m.request_id = r.request_id
                    WHERE r.status = 'matched'
                      AND m.estimated_dropoff_time <= ?
                """, (current_time,))

                to_complete = cursor.fetchall()

                completed_count = 0
                for row in to_complete:
                    if self.matching_system.complete_trip(row['request_id'], row['vehicle_id']):
                        completed_count += 1

                return completed_count

        except Exception as e:
            logger.error(f"Error completing rides: {e}")
            return 0

    def run_simulation(self):
        """Run the full simulation."""
        logger.info("=" * 60)
        logger.info("Starting Shared-Ride Matching Simulation")
        logger.info("=" * 60)

        # Initialize
        self.initialize_vehicles()
        request_ids = self.generate_requests()

        if self.sumo_gui:
            self.initialize_sumo()
            self.sumo.start_simulation()
            self.sumo.initialize_distances()

        # Simulation loop
        start_time = time.time()
        current_sim_time = 0
        now = datetime.now()

        metrics_interval = 600  # Log metrics every 600 seconds (10 minutes)

        logger.info("Starting simulation loop...")

        try:
            while current_sim_time < self.config['simulation_time']:
                # Step SUMO
                if self.sumo and self.sumo.connected:
                    self.sumo.step(int(self.config['time_step']))
                    self.sumo.update_distances()

                # Process requests
                current_time = now + timedelta(seconds=current_sim_time)

                # Process pending requests
                matched = self.process_pending_requests(current_time)

                # Complete rides
                completed = self.complete_rides(current_time)

                # Log progress
                if current_sim_time % 300 == 0:  # Every 5 minutes
                    logger.info(
                        f"Time: {current_sim_time}s/{self.config['simulation_time']}s | "
                        f"Matched: {matched} | Completed: {completed}"
                    )

                # Log metrics at intervals
                if current_sim_time > 0 and current_sim_time % metrics_interval == 0:
                    self.log_metrics(current_sim_time)

                current_sim_time += self.config['time_step']

        except KeyboardInterrupt:
            logger.info("Simulation interrupted by user")

        finally:
            # Close SUMO
            if self.sumo:
                self.sumo.close()

            # Final metrics
            elapsed_real = time.time() - start_time
            logger.info(f"Simulation completed in {elapsed_real:.2f} seconds")

            # Generate final report
            self.generate_final_report()

    def log_metrics(self, current_time: float):
        """Log current metrics."""
        metrics = self.matching_system.get_system_metrics()
        logger.info(
            f"[{current_time}s] AVO: {metrics['average_vehicle_occupancy']:.3f} | "
            f"Match Rate: {metrics['match_rate']:.1f}% | "
            f"Timeout Rate: {metrics['timeout_rate']:.1f}% | "
            f"Available: {metrics['available_vehicles']}/{metrics['total_vehicles']}"
        )

    def generate_final_report(self):
        """Generate and display final performance report."""
        logger.info("=" * 60)
        logger.info("FINAL PERFORMANCE REPORT")
        logger.info("=" * 60)

        # Get comprehensive metrics
        report = self.evaluator.generate_full_report("performance_report.json")
        metrics = report['metrics']

        # Print key metrics
        print("\n" + "=" * 60)
        print("KEY PERFORMANCE METRICS")
        print("=" * 60)

        print(f"\n1. Average Vehicle Occupancy (AVO):")
        print(f"   {metrics['average_vehicle_occupancy'] * 100:.2f}%")

        print(f"\n2. Vehicle Miles Traveled (VMT):")
        vmt = metrics['vmt']
        print(f"   Total Rides: {vmt['total_rides']}")
        print(f"   Shared Rides: {vmt['shared_rides']}")
        print(f"   Sharing Ratio: {vmt['sharing_ratio'] * 100:.1f}%")
        print(f"   VMT Reduction: {vmt['vmt_reduction_percent']:.2f}%")

        print(f"\n3. Request Processing:")
        rr = metrics['rejection_rates']
        print(f"   Total Requests: {rr['total_requests']}")
        print(f"   Match Rate: {rr['matched_rate']:.2f}%")
        print(f"   Completion Rate: {rr['completion_rate']:.2f}%")
        print(f"   Rejection Rate: {rr['rejection_rate']:.2f}%")
        print(f"   Timeout Rate: {rr['timeout_rate']:.2f}%")

        print(f"\n4. Transaction Integrity:")
        tm = metrics['transaction_metrics']
        print(f"   Total Transactions: {tm['total_transactions']}")
        print(f"   Successful: {tm['successful_transactions']}")
        print(f"   Failed: {tm['failed_transactions']}")

        print(f"\n5. Passenger Experience:")
        wt = metrics['wait_times']
        print(f"   Avg Wait Time: {wt['avg_wait_time_seconds']:.1f}s")
        print(f"   Min Wait Time: {wt['min_wait_time_seconds']:.1f}s")
        print(f"   Max Wait Time: {wt['max_wait_time_seconds']:.1f}s")

        print("\n" + "=" * 60)
        print("SCALE PARAMETERS")
        print("=" * 60)
        scale = report['scale_info']
        print(f"Network: {scale['network_size']}")
        print(f"Vehicles: {scale['vehicles']}")
        print(f"Requests: {scale['requests']}")
        print(f"Seats/Vehicle: {scale['seats_per_vehicle']}")
        print(f"Simulation Window: {scale['simulation_window']}")

        print("\n" + "=" * 60)

        # Generate visualization
        try:
            self.evaluator.plot_metrics(metrics, "performance_metrics.png")
            print("Visualization saved to performance_metrics.png")
        except Exception as e:
            logger.warning(f"Could not generate plot: {e}")

        # Save report
        print(f"Report saved to performance_report.json")


def run_standalone_test():
    """Run a simplified test without SUMO."""
    logger.info("Running standalone test (no SUMO)...")

    sim = SharedRideSimulation(sumo_gui=False)
    sim.initialize_vehicles()
    request_ids = sim.generate_requests()

    # Process all requests
    start_time = time.time()
    processed = 0
    timeouts = 0

    now = datetime.now()

    for i, request_id in enumerate(request_ids):
        if i % 100 == 0:
            logger.info(f"Processing request {i}/{len(request_ids)}")

        success, result = sim.matching_system.attempt_match_with_transaction(
            request_id,
            max_retries=3,
            timeout_ms=1000
        )

        if success:
            processed += 1
        else:
            if "timeout" in str(result.get("error", "")).lower():
                timeouts += 1

    elapsed = time.time() - start_time

    logger.info(f"Processed {processed}/{len(request_ids)} requests")
    logger.info(f"Timeouts: {timeouts}")
    logger.info(f"Elapsed time: {elapsed:.2f}s")

    # Generate final report
    sim.generate_final_report()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Shared-Ride Matching System')
    parser.add_argument('--gui', action='store_true', help='Run SUMO with GUI')
    parser.add_argument('--no-sumo', action='store_true', help='Run without SUMO')
    parser.add_argument('--benchmark', action='store_true', help='Run stress test benchmark')

    args = parser.parse_args()

    if args.benchmark:
        logger.info("Running benchmark...")
        sim = SharedRideSimulation(sumo_gui=False)
        sim.initialize_vehicles()

        benchmark = Benchmark(sim.matching_system, sim.evaluator)
        results = benchmark.run_stress_test(num_requests=3000, batch_size=20)

        print("\nBenchmark Results:")
        print(f"Total Requests: {results['total_requests']}")
        print(f"Successful Matches: {results['successful_matches']}")
        print(f"Timeouts: {results['timeouts']}")
        print(f"Match Rate: {results['match_rate']:.2f}%")
        print(f"Timeout Rate: {results['timeout_rate']:.2f}%")
        print(f"Throughput: {results['requests_per_second']:.2f} req/s")

    elif args.no_sumo:
        run_standalone_test()

    else:
        sim = SharedRideSimulation(sumo_gui=args.gui)
        sim.run_simulation()


if __name__ == "__main__":
    main()
