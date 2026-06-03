-- schema
CREATE SCHEMA IF NOT EXISTS diagnosis;

-- drop existing table
DROP TABLE IF EXISTS diagnosis.attributes CASCADE;

-- create table
CREATE TABLE diagnosis.attributes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icd_11_id VARCHAR(255) NOT NULL UNIQUE,
    icd_11_code VARCHAR(10),
    name VARCHAR(255) NOT NULL
);

-- Add index(es)
CREATE INDEX idx_attributes_icd_code
ON diagnosis.attributes(icd_11_code);

