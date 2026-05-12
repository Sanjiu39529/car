"""
SUMO TraCI Integration for Dynamic Rerouting in Shared-Ride Matching
Handles communication with SUMO simulation and manages vehicle routes
"""

import traci
import logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# Define fallback functions if numpy is not available
def sqrt(x):
    if HAS_NUMPY:
        return np.sqrt(x)
    return x ** 0.5

def mean(values):
    if HAS_NUMPY:
        return np.mean(values)
    return sum(values) / len(values) if values else 0

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class RouteStop:
    edge: str
    position: float
    passenger_id: str
    stop_type: str  # 'pickup' or 'dropoff'


class SUMOIntegration:
    def __init__(self, sumo_cmd: List[str], port: int = 8813):
        self.sumo_cmd = sumo_cmd
        self.port = port
        self.connected = False
        self.vehicle_routes: Dict[str, List[RouteStop]] = {}
        self.initial_distance = {}
        self.current_distance = {}

    def start_simulation(self):
        """Start SUMO with TraCI."""
        try:
            logger.info(f"Starting SUMO with command: {' '.join(self.sumo_cmd)}")
            traci.start(self.sumo_cmd)
            self.connected = True
            logger.info("SUMO TraCI connected successfully")
        except Exception as e:
            logger.error(f"Failed to start SUMO: {e}")
            raise

    def close(self):
        """Close TraCI connection."""
        if self.connected:
            traci.close()
            self.connected = False
            logger.info("SUMO TraCI connection closed")

    def get_vehicle_position(self, vehicle_id: str) -> Tuple[float, float]:
        """Get current x,y position of a vehicle."""
        if not self.connected:
            return (0.0, 0.0)
        x, y = traci.vehicle.getPosition(vehicle_id)
        return (x, y)

    def get_vehicle_edge(self, vehicle_id: str) -> str:
        """Get current edge of a vehicle."""
        if not self.connected:
            return ""
        return traci.vehicle.getRoadID(vehicle_id)

    def get_vehicle_speed(self, vehicle_id: str) -> float:
        """Get current speed of a vehicle in m/s."""
        if not self.connected:
            return 0.0
        return traci.vehicle.getSpeed(vehicle_id)

    def add_intermediate_stop(self, vehicle_id: str, edge: str, position: float,
                             stop_type: str, passenger_id: str, duration: float = 30.0) -> bool:
        """
        Add an intermediate stop to a vehicle's route.
        This implements Dynamic Rerouting for shared-ride matches.
        """
        try:
            if not self.connected:
                logger.warning("SUMO not connected, cannot add stop")
                return False

            # Record the stop
            stop = RouteStop(edge=edge, position=position, passenger_id=passenger_id, stop_type=stop_type)
            if vehicle_id not in self.vehicle_routes:
                self.vehicle_routes[vehicle_id] = []

            self.vehicle_routes[vehicle_id].append(stop)

            # Add stop to vehicle in SUMO
            traci.vehicle.setStop(
                vehID=vehicle_id,
                edgeID=edge,
                pos=position,
                laneIndex=0,
                duration=duration,
                flags=traci.constants.STOP_PARKING
            )

            logger.info(f"Added {stop_type} stop for passenger {passenger_id} on edge {edge}")
            return True

        except Exception as e:
            logger.error(f"Failed to add intermediate stop: {e}")
            return False

    def reroute_vehicle_to_location(self, vehicle_id: str, target_edge: str) -> bool:
        """
        Reroute a vehicle to a specific edge for pickup/dropoff.
        """
        try:
            if not self.connected:
                return False

            # Get current edge
            current_edge = self.get_vehicle_edge(vehicle_id)

            # Calculate new route
            route = traci.simulation.findIntermodalRoute(
                fromEdge=current_edge,
                toEdge=target_edge,
                modes='public',
                depart='now',
                routingMode=0
            )

            if route:
                # Extract edges from route
                new_route_edges = [edge_id for edge_id, _ in route[0]]
                traci.vehicle.setRoute(vehicle_id, new_route_edges)
                logger.info(f"Rerouted vehicle {vehicle_id} from {current_edge} to {target_edge}")
                return True
            else:
                logger.warning(f"Could not find route from {current_edge} to {target_edge}")
                return False

        except Exception as e:
            logger.error(f"Failed to reroute vehicle: {e}")
            return False

    def get_vehicle_distance_traveled(self, vehicle_id: str) -> float:
        """Get total distance traveled by a vehicle in meters."""
        if not self.connected:
            return 0.0
        return traci.vehicle.getDistance(vehicle_id)

    def initialize_distances(self):
        """Store initial distances for VMT calculation."""
        if not self.connected:
            return
        vehicle_ids = traci.vehicle.getIDList()
        for vid in vehicle_ids:
            self.initial_distance[vid] = 0.0
            self.current_distance[vid] = 0.0

    def update_distances(self):
        """Update current distances for all vehicles."""
        if not self.connected:
            return
        for vid in traci.vehicle.getIDList():
            self.current_distance[vid] = traci.vehicle.getDistance(vid)

    def calculate_vmt_reduction(self, total_requests: int) -> Dict[str, float]:
        """
        Calculate Vehicle Miles Traveled (VMT) reduction.
        Compares shared rides vs individual rides.
        """
        if not self.connected:
            return {"current_vmt": 0.0, "baseline_vmt": 0.0, "reduction": 0.0}

        # Current VMT: sum of all vehicle distances
        current_vmt = sum(self.current_distance.values()) / 1609.34  # Convert to miles

        # Baseline: assume each request requires a separate vehicle
        # Use average distance per vehicle
        if HAS_NUMPY:
            avg_distance = mean(list(self.current_distance.values())) if self.current_distance else 0
        else:
            avg_distance = sum(self.current_distance.values()) / len(self.current_distance) if self.current_distance else 0
        baseline_vmt = (total_requests * avg_distance) / 1609.34

        if baseline_vmt > 0:
            reduction = ((baseline_vmt - current_vmt) / baseline_vmt) * 100
        else:
            reduction = 0.0

        return {
            "current_vmt": current_vmt,
            "baseline_vmt": baseline_vmt,
            "reduction": reduction
        }

    def get_simulation_time(self) -> float:
        """Get current simulation time in seconds."""
        if not self.connected:
            return 0.0
        return traci.simulation.getTime()

    def step(self, steps: int = 1):
        """Advance simulation by specified number of steps."""
        if not self.connected:
            return
        for _ in range(steps):
            traci.simulationStep()

    def get_all_vehicles(self) -> List[str]:
        """Get list of all vehicle IDs in simulation."""
        if not self.connected:
            return []
        return traci.vehicle.getIDList()

    def get_vehicle_passengers(self, vehicle_id: str) -> int:
        """Get number of passengers in a vehicle."""
        if not self.connected:
            return 0
        return traci.vehicle.getPersonNumber(vehicle_id)

    def set_vehicle_speed(self, vehicle_id: str, speed: float):
        """Set vehicle speed (for testing/debugging)."""
        if not self.connected:
            return
        traci.vehicle.setSpeed(vehicle_id, speed)

    def create_vehicle(self, vehicle_id: str, route_id: str, type_id: str = "passenger"):
        """Create a new vehicle in SUMO."""
        try:
            if not self.connected:
                return False

            traci.vehicle.add(
                vehID=vehicle_id,
                routeID=route_id,
                typeID=type_id,
                depart="now"
            )

            logger.info(f"Created vehicle {vehicle_id} on route {route_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to create vehicle: {e}")
            return False

    def remove_vehicle(self, vehicle_id: str):
        """Remove a vehicle from simulation."""
        try:
            if not self.connected:
                return
            traci.vehicle.remove(vehicle_id)
            logger.info(f"Removed vehicle {vehicle_id}")
        except Exception as e:
            logger.error(f"Failed to remove vehicle: {e}")

    def get_edge_ids(self) -> List[str]:
        """Get list of all edge IDs in the network."""
        if not self.connected:
            return []
        return traci.edge.getIDList()

    def get_random_edge(self, exclude: List[str] = None) -> Optional[str]:
        """Get a random edge from the network."""
        if not self.connected:
            return None

        edges = self.get_edge_ids()
        if exclude:
            edges = [e for e in edges if e not in exclude]

        if edges:
            # Filter out internal edges
            edges = [e for e in edges if not e.startswith(':')]
            if edges:
                import random
                return random.choice(edges)

        return None

    def get_edge_length(self, edge_id: str) -> float:
        """Get length of an edge in meters."""
        if not self.connected:
            return 0.0
        return traci.edge.getLength(edge_id)

    def find_nearest_edge(self, x: float, y: float) -> Optional[str]:
        """Find the nearest edge to a given x,y position."""
        if not self.connected:
            return None
        return traci.simulation.convertRoad(x, y)

    def get_edge_position(self, edge_id: str, position: float = 0.5) -> Tuple[float, float]:
        """Get x,y coordinates at a position along an edge (0-1)."""
        if not self.connected:
            return (0.0, 0.0)

        length = self.get_edge_length(edge_id)
        pos = position * length

        shape = traci.edge.getShape(edge_id)
        if not shape:
            return (0.0, 0.0)

        # Simple linear interpolation
        total_length = sum(
            sqrt((shape[i+1][0] - shape[i][0])**2 + (shape[i+1][1] - shape[i][1])**2)
            for i in range(len(shape)-1)
        )

        if total_length == 0:
            return shape[0] if shape else (0.0, 0.0)

        current_dist = 0.0
        for i in range(len(shape)-1):
            segment_length = sqrt(
                (shape[i+1][0] - shape[i][0])**2 + (shape[i+1][1] - shape[i][1])**2
            )
            if current_dist + segment_length >= pos:
                # Interpolate within this segment
                t = (pos - current_dist) / segment_length if segment_length > 0 else 0
                return (
                    shape[i][0] + t * (shape[i+1][0] - shape[i][0]),
                    shape[i][1] + t * (shape[i+1][1] - shape[i][1])
                )
            current_dist += segment_length

        return shape[-1] if shape else (0.0, 0.0)