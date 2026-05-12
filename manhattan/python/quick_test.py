#!/usr/bin/env python3
"""
Quick test of the shared-ride matching system
"""

from ride_matching import RideMatchingSystem

def main():
    print("Shared-Ride Matching System - Quick Test")
    print("=" * 40)

    # Initialize system
    ms = RideMatchingSystem("test_db.db")

    # Add a few vehicles
    ms.add_vehicle("v1", "edge_1", 4)
    ms.add_vehicle("v2", "edge_2", 4)
    print("Added 2 vehicles")

    # Add a trip request
    from datetime import datetime, timedelta
    now = datetime.now()
    request_id = ms.add_trip_request("p1", "edge_1", "edge_3",
                                     now, now + timedelta(minutes=10))
    print(f"Added trip request: {request_id}")

    # Match the request
    success, result = ms.attempt_match_with_transaction(request_id)
    if success:
        print(f"Successfully matched to vehicle: {result['vehicle_id']}")
    else:
        print(f"Failed to match: {result.get('error', 'unknown error')}")

    # Get system metrics
    metrics = ms.get_system_metrics()
    print("\nCurrent Metrics:")
    print(f"  AVO: {metrics['average_vehicle_occupancy']:.3f}")
    print(f"  Available Vehicles: {metrics['available_vehicles']}/{metrics['total_vehicles']}")
    print(f"  Total Requests: {metrics['total_requests']}")
    print(f"  Matched Rate: {metrics['match_rate']:.1f}%")

    print("\nQuick test completed successfully!")

if __name__ == "__main__":
    main()