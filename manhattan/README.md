# Transactional Integrity in Shared-Ride Matching

A comprehensive shared-ride matching system that maximizes vehicle occupancy through complex relational constraints without overbooking. The system demonstrates transactional integrity, dynamic rerouting via TraCI, and real-time performance evaluation.

## Features

### Core Capabilities

- **Relational Schema Design**: Complete database schema for Vehicles, Current_Passengers, and Trip_Requests with seat-count constraints
- **Overlap Matching**: SQL queries that identify passengers with overlapping origin-destination paths within configurable time windows
- **Capacity Enforcement**: CHECK constraints and ACID transactions ensuring vehicles never exceed capacity
- **Dynamic Rerouting**: TraCI integration for adding intermediate stops to vehicle routes upon matching
- **Preference Filtering**: User preference support (e.g., "maximum 1 extra stop") in SQL matching queries
- **Performance Evaluation**: Real-time metrics including Average Vehicle Occupancy (AVO) and VMT Reduction

### Transactional Integrity

- ACID-compliant SQLite database
- Explicit capacity constraint: `current_load + new_request <= capacity`
- Automatic rollback on transaction failures
- Transaction logging for audit trails
- Timeout handling during peak load periods

## Architecture

```
manhattan/
├── data/
│   ├── net.net.xml              # SUMO network file
│   ├── routes.xml               # Original routes
│   ├── routes_ride_sharing.xml  # Shared-ride vehicle routes
│   ├── manhattan.sumocfg        # SUMO configuration
│   ├── manhattan.jtrrcfg        # SUMO GUI settings
│   └── manhattan.netgcfg        # Network generation config
├── python/
│   ├── ride_matching.py         # Core matching logic
│   ├── traci_integration.py     # SUMO TraCI integration
│   ├── evaluation.py            # Performance metrics
│   └── main.py                  # Entry point
├── schema.sql                   # Database schema
├── requirements.txt             # Python dependencies
└── README.md                    # This file
```

## Performance Scale

- **Network**: 5km × 5km mixed-use area (Manhattan grid)
- **Vehicles**: 500 shared-ride vehicles
- **Trip Requests**: 3,000 requests per simulation window
- **Vehicle Capacity**: 4 seats per vehicle
- **Simulation Window**: 7,200 seconds (2 hours)

## Key Metrics

- **Average Vehicle Occupancy (AVO)**: Target > 70%
- **VMT Reduction**: Target > 40% vs. individual rides
- **Rejection Rate**: Monitored during peak matching bursts
- **Transaction Integrity**: 100% with proper timeout handling

## Installation

### Prerequisites

- Python 3.9+
- SUMO (Simulation of Urban MObility) 1.20.0+

### Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd manhattan
```

2. Install Python dependencies:
```bash
pip install -r requirements.txt
```

3. Ensure SUMO is installed and in your PATH:
```bash
sumo --version
```

## Usage

### Run Full Simulation

```bash
# Run without SUMO GUI (recommended for production)
cd python
python main.py --no-sumo

# Run with SUMO GUI (for visualization)
python main.py --gui

# Run stress test benchmark
python main.py --benchmark
```

### Standalone Test (Database Only)

```bash
cd python
python main.py --no-sumo
```

This runs the matching system without SUMO, testing only the database operations.

### Benchmark Testing

```bash
cd python
python main.py --benchmark
```

This stress tests the transaction system under load (3,000 concurrent requests).

## Module Descriptions

### `ride_matching.py`

Core matching logic with:

- `RideMatchingSystem`: Main class for ride matching operations
- Transaction-safe request processing
- Capacity constraint enforcement
- Overlap detection and matching
- System-wide metrics calculation

### `traci_integration.py`

SUMO TraCI interface:

- `SUMOIntegration`: TraCI connection management
- Dynamic route modification
- Stop management (pickup/dropoff)
- Vehicle position tracking
- VMT calculation

### `evaluation.py`

Performance evaluation:

- `PerformanceEvaluator`: Metrics calculation
- AVO computation
- VMT reduction analysis
- Rejection rate tracking
- Visualization generation

### `main.py`

Simulation orchestrator:

- Initialization and setup
- Request generation
- Simulation loop coordination
- Report generation

## Database Schema

### Vehicles Table

```sql
CREATE TABLE Vehicles (
    vehicle_id TEXT PRIMARY KEY,
    current_location TEXT NOT NULL,
    total_capacity INTEGER NOT NULL CHECK (total_capacity > 0),
    current_load INTEGER DEFAULT 0 CHECK (current_load >= 0),
    route_id TEXT,
    status TEXT DEFAULT 'available',
    CONSTRAINT capacity_constraint CHECK (current_load <= total_capacity)
);
```

### Trip_Requests Table

```sql
CREATE TABLE Trip_Requests (
    request_id TEXT PRIMARY KEY,
    passenger_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    pickup_window_start TIMESTAMP NOT NULL,
    pickup_window_end TIMESTAMP NOT NULL,
    max_extra_stops INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending'
);
```

### Current_Passengers Table

```sql
CREATE TABLE Current_Passengers (
    passenger_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    pickup_time TIMESTAMP NOT NULL,
    pickup_location TEXT NOT NULL,
    destination TEXT NOT NULL,
    is_en_route BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (passenger_id, vehicle_id)
);
```

## Transaction Safety

The system uses SQLite with:

- **Immediate Transactions**: `BEGIN IMMEDIATE` for write locks
- **Busy Timeout**: 5-second retry on database locks
- **Explicit Capacity Check**: `current_load + 1 <= total_capacity`
- **Automatic Rollback**: On any constraint violation
- **Transaction Logging**: Full audit trail

## Example Output

```
============================================================
FINAL PERFORMANCE REPORT
============================================================

1. Average Vehicle Occupancy (AVO):
   78.45%

2. Vehicle Miles Traveled (VMT):
   Total Rides: 2947
   Shared Rides: 1856
   Sharing Ratio: 63.01%
   VMT Reduction: 47.23%

3. Request Processing:
   Total Requests: 3000
   Match Rate: 98.23%
   Completion Rate: 95.67%
   Rejection Rate: 1.77%
   Timeout Rate: 0.12%

4. Transaction Integrity:
   Total Transactions: 8941
   Successful: 8935
   Failed: 6
```

## Contributing

This is a research implementation for transactional integrity in shared-ride matching. Key areas for extension:

1. Advanced matching algorithms (e.g., predictive matching)
2. Real-time edge integration (e.g., GPS feeds)
3. Multi-modal transportation support
4. Dynamic pricing models
5. Machine learning-based demand prediction

## License

MIT License - See LICENSE file for details

## Citation

If you use this code in your research, please cite:

```
Transactional Integrity in Shared-Ride Matching:
Maximizing Vehicle Occupancy Through Relational Constraints
```

## Contact

For questions or issues, please open an issue on GitHub.
