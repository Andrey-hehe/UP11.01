CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(120) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'renter')),
    driver_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE drivers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(30) NOT NULL,
    license VARCHAR(30) UNIQUE NOT NULL,
    experience INTEGER NOT NULL DEFAULT 0 CHECK (experience >= 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'vacation', 'fired'))
);

ALTER TABLE users
    ADD CONSTRAINT users_driver_fk FOREIGN KEY (driver_id)
    REFERENCES drivers(id) ON DELETE RESTRICT;

CREATE TABLE cars (
    id SERIAL PRIMARY KEY,
    brand VARCHAR(50) NOT NULL,
    model VARCHAR(50) NOT NULL,
    plate VARCHAR(20) UNIQUE NOT NULL,
    year INTEGER NOT NULL CHECK (year BETWEEN 1900 AND 2100),
    vin VARCHAR(17) UNIQUE NOT NULL,
    daily_rate NUMERIC(10, 2) NOT NULL CHECK (daily_rate > 0),
    status TEXT NOT NULL DEFAULT 'free' CHECK (status IN ('free', 'reserved', 'rented', 'repair'))
);

CREATE TABLE rentals (
    id SERIAL PRIMARY KEY,
    car_id INTEGER NOT NULL REFERENCES cars(id),
    renter_id INTEGER NOT NULL REFERENCES users(id),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL CHECK (end_date > start_date),
    total_price NUMERIC(10, 2) NOT NULL CHECK (total_price >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending_payment', 'active', 'completed', 'cancelled')),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    paid_at TIMESTAMP
);

CREATE UNIQUE INDEX one_open_rental_per_user ON rentals(renter_id)
    WHERE status IN ('pending_payment', 'active');
CREATE UNIQUE INDEX one_open_rental_per_car ON rentals(car_id)
    WHERE status IN ('pending_payment', 'active');

CREATE TABLE car_driver (
    id SERIAL PRIMARY KEY,
    car_id INTEGER NOT NULL REFERENCES cars(id),
    driver_id INTEGER NOT NULL REFERENCES drivers(id),
    rental_id INTEGER NOT NULL UNIQUE REFERENCES rentals(id),
    assigned_date TIMESTAMP NOT NULL DEFAULT now(),
    unassigned_date TIMESTAMP
);

CREATE UNIQUE INDEX one_active_driver ON car_driver(driver_id)
    WHERE unassigned_date IS NULL;
CREATE UNIQUE INDEX one_active_car ON car_driver(car_id)
    WHERE unassigned_date IS NULL;

CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    rental_id INTEGER NOT NULL REFERENCES rentals(id),
    amount NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed')),
    transaction_number VARCHAR(50) UNIQUE NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    paid_at TIMESTAMP
);

CREATE TABLE maintenance (
    id SERIAL PRIMARY KEY,
    car_id INTEGER NOT NULL REFERENCES cars(id),
    service_date DATE NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('TO', 'repair')),
    cost NUMERIC(10, 2) NOT NULL CHECK (cost >= 0),
    description TEXT NOT NULL DEFAULT '',
    is_completed BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE fuel_records (
    id SERIAL PRIMARY KEY,
    car_id INTEGER NOT NULL REFERENCES cars(id),
    fuel_date DATE NOT NULL,
    liters NUMERIC(8, 2) NOT NULL CHECK (liters > 0),
    cost NUMERIC(10, 2) NOT NULL CHECK (cost >= 0)
);

INSERT INTO cars (brand, model, plate, year, vin, daily_rate) VALUES
    ('Hyundai', 'Solaris', 'А123ВС777', 2021, 'Z94K241BBMR123456', 1800),
    ('Kia', 'Rio', 'К456МН777', 2022, 'Z94C241BBNR123456', 2000),
    ('Volkswagen', 'Polo', 'О789РТ777', 2020, 'XW8ZZZ61ZLG123456', 1900);
