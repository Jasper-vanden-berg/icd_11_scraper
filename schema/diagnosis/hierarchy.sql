-- schema
CREATE SCHEMA IF NOT EXISTS diagnosis;

-- drop existing table
DROP TABLE IF EXISTS diagnosis.diagnosis_hierarchy CASCADE;

-- create table
CREATE TABLE diagnosis.diagnosis_hierarchy (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ancestor BIGINT NOT NULL,
    descendant BIGINT NOT NULL,
    depth SMALLINT NOT NULL,

    -- Add relationships
    CONSTRAINT fk_relationship_ancestor
        FOREIGN KEY (ancestor)
        REFERENCES diagnosis.diagnosis(id),

    CONSTRAINT fk_relationship_descendant
        FOREIGN KEY (descendant)
        REFERENCES diagnosis.diagnosis(id),  

    -- Add unique constraints
    CONSTRAINT uq_diag_hierarchy
        UNIQUE (ancestor, descendant)
);

-- Add index(es)
CREATE INDEX idx_diagnosis_hierarchy_ancestor
ON diagnosis.diagnosis_hierarchy(ancestor);

CREATE INDEX idx_diagnosis_hierarchy_descendant
ON diagnosis.diagnosis_hierarchy(descendant);

CREATE INDEX idx_diagnosis_hierarchy_depth
ON diagnosis.diagnosis_hierarchy(depth);