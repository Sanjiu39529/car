-- =====================================================
-- Transactional Integrity in Shared-Ride Matching
-- Relational Schema Design
-- =====================================================

-- Vehicles Table: Stores vehicle information and capacity
CREATE TABLE Vehicles (
    vehicle_id TEXT PRIMARY KEY,
    current_location TEXT NOT NULL,
    total_capacity INTEGER NOT NULL CHECK (total_capacity > 0),
    current_load INTEGER DEFAULT 0 CHECK (current_load >= 0),
    route_id TEXT,
    status TEXT DEFAULT 'available' CHECK (status IN ('available', 'busy', 'offline')),
    last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT capacity_constraint CHECK (current_load <= total_capacity)
);

-- Trip_Requests Table: Stores passenger ride requests
CREATE TABLE Trip_Requests (
    request_id TEXT PRIMARY KEY,
    passenger_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pickup_window_start TIMESTAMP NOT NULL,
    pickup_window_end TIMESTAMP NOT NULL,
    max_extra_stops INTEGER DEFAULT 1 CHECK (max_extra_stops >= 0),
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'matched', 'completed', 'rejected', 'timeout')),
    preferred_vehicle_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT valid_pickup_window CHECK (pickup_window_end > pickup_window_start)
);

-- Current_Passengers Table: Tracks passengers currently in vehicles
CREATE TABLE Current_Passengers (
    passenger_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    pickup_time TIMESTAMP NOT NULL,
    pickup_location TEXT NOT NULL,
    destination TEXT NOT NULL,
    expected_dropoff_time TIMESTAMP,
    actual_dropoff_time TIMESTAMP,
    is_en_route BOOLEAN DEFAULT FALSE,
    stop_sequence INTEGER,
    PRIMARY KEY (passenger_id, vehicle_id),
    FOREIGN KEY (vehicle_id) REFERENCES Vehicles(vehicle_id),
    FOREIGN KEY (request_id) REFERENCES Trip_Requests(request_id),
    CONSTRAINT valid_dropoff CHECK (actual_dropoff_time IS NULL OR actual_dropoff_time >= pickup_time)
);

-- Matches Table: Tracks successful ride matches
CREATE TABLE Matches (
    match_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    estimated_pickup_time TIMESTAMP NOT NULL,
    estimated_dropoff_time TIMESTAMP NOT NULL,
    shared BOOLEAN DEFAULT FALSE,
    pickup_stop_sequence INTEGER,
    dropoff_stop_sequence INTEGER,
    FOREIGN KEY (vehicle_id) REFERENCES Vehicles(vehicle_id),
    FOREIGN KEY (request_id) REFERENCES Trip_Requests(request_id)
);

-- Route_Stops Table: Tracks vehicle routes with intermediate stops
CREATE TABLE Route_Stops (
    stop_id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL,
    location TEXT NOT NULL,
    stop_type TEXT CHECK (stop_type IN ('pickup', 'dropoff')),
    passenger_id TEXT,
    scheduled_time TIMESTAMP NOT NULL,
    actual_time TIMESTAMP,
    completed BOOLEAN DEFAULT FALSE,
    stop_sequence INTEGER NOT NULL,
    FOREIGN KEY (vehicle_id) REFERENCES Vehicles(vehicle_id),
    FOREIGN KEY (passenger_id) REFERENCES Current_Passengers(passenger_id)
);

-- Transaction_Log Table: Logs all transactions for rollback support
CREATE TABLE Transaction_Log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id TEXT,
    old_values TEXT,
    new_values TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Performance_Metrics Table: Stores evaluation metrics
CREATE TABLE Performance_Metrics (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    window_start TIMESTAMP NOT NULL,
    window_end TIMESTAMP NOT NULL
);

-- Indexes for query optimization
CREATE INDEX idx_trip_requests_origin ON Trip_Requests(origin);
CREATE INDEX idx_trip_requests_destination ON Trip_Requests(destination);
CREATE INDEX idx_trip_requests_time_window ON Trip_Requests(pickup_window_start, pickup_window_end);
CREATE INDEX idx_trip_requests_status ON Trip_Requests(status);
CREATE INDEX idx_current_passengers_vehicle ON Current_Passengers(vehicle_id);
CREATE INDEX idx_route_stops_vehicle ON Route_Stops(vehicle_id);
CREATE INDEX idx_vehicles_status ON Vehicles(status);

-- Triggers for maintaining transactional integrity

-- Trigger: Ensure vehicle capacity is not exceeded
CREATE TRIGGER check_capacity_before_insert
BEFORE INSERT ON Current_Passengers
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN (SELECT current_load FROM Vehicles WHERE vehicle_id = NEW.vehicle_id) >=
             (SELECT total_capacity FROM Vehicles WHERE vehicle_id = NEW.vehicle_id)
        THEN RAISE(ABORT, 'Vehicle capacity exceeded')
    END;
END;

-- Trigger: Update vehicle load when passenger is added
CREATE TRIGGER update_load_on_passenger_add
AFTER INSERT ON Current_Passengers
WHEN NEW.is_en_route = TRUE
BEGIN
    UPDATE Vehicles
    SET current_load = current_load + 1
    WHERE vehicle_id = NEW.vehicle_id;
END;

-- Trigger: Update vehicle load when passenger is dropped off
CREATE TRIGGER update_load_on_passenger_remove
AFTER UPDATE ON Current_Passengers
WHEN OLD.is_en_route = TRUE AND NEW.is_en_route = FALSE
BEGIN
    UPDATE Vehicles
    SET current_load = current_load - 1
    WHERE vehicle_id = NEW.vehicle_id;
END;

-- Trigger: Log changes to Vehicles table
CREATE TRIGGER log_vehicle_changes
AFTER UPDATE ON Vehicles
BEGIN
    INSERT INTO Transaction_Log (transaction_id, operation, table_name, record_id, old_values, new_values)
    VALUES (
        hex(randomblob(16)),
        'UPDATE',
        'Vehicles',
        NEW.vehicle_id,
        json_object('current_load', OLD.current_load, 'status', OLD.status),
        json_object('current_load', NEW.current_load, 'status', NEW.status)
    );
END;

-- Views for common queries

-- View: Available vehicles with capacity info
CREATE VIEW Available_Vehicles AS
SELECT
    v.vehicle_id,
    v.current_location,
    v.total_capacity,
    v.current_load,
    (v.total_capacity - v.current_load) as available_seats,
    v.route_id
FROM Vehicles v
WHERE v.status = 'available' AND v.current_load < v.total_capacity;

-- View: Active trip requests
CREATE VIEW Active_Requests AS
SELECT
    r.request_id,
    r.passenger_id,
    r.origin,
    r.destination,
    r.pickup_window_start,
    r.pickup_window_end,
    r.max_extra_stops,
    r.status
FROM Trip_Requests r
WHERE r.status IN ('pending', 'matched');

-- View: Overlapping trip requests for potential sharing
CREATE VIEW Overlapping_Requests AS
SELECT
    r1.request_id as request_id_1,
    r1.origin as origin_1,
    r1.destination as destination_1,
    r1.pickup_window_start as window_start_1,
    r1.pickup_window_end as window_end_1,
    r2.request_id as request_id_2,
    r2.origin as origin_2,
    r2.destination as destination_2,
    r2.pickup_window_start as window_start_2,
    r2.pickup_window_end as window_end_2,
    julianday(r1.pickup_window_end) - julianday(r2.pickup_window_start) as time_diff_days
FROM Trip_Requests r1
JOIN Trip_Requests r2 ON r1.request_id != r2.request_id
WHERE r1.status = 'pending'
  AND r2.status = 'pending'
  AND (
      (r1.origin = r2.origin) OR
      (r1.destination = r2.destination) OR
      (r1.origin = r2.destination) OR
      (r1.destination = r2.origin)
  )
  AND ABS(julianday(r1.pickup_window_end) - julianday(r2.pickup_window_start)) * 86400 <= 600; -- 10 minute overlap threshold
