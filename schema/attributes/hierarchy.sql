-- schema
CREATE SCHEMA IF NOT EXISTS diagnosis;

-- drop existing table
DROP TABLE IF EXISTS diagnosis.attributes_hierarchy CASCADE;

-- create table
CREATE TABLE diagnosis.attributes_hierarchy (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ancestor BIGINT NOT NULL,
    descendant BIGINT NOT NULL,
    depth SMALLINT NOT NULL,

    -- Add relationships
    CONSTRAINT fk_relationship_ancestor
        FOREIGN KEY (ancestor)
        REFERENCES diagnosis.attributes(id),

    CONSTRAINT fk_relationship_descendant
        FOREIGN KEY (descendant)
        REFERENCES diagnosis.attributes(id),  

    -- Add unique constraints
    CONSTRAINT uq_attr_hierarchy
        UNIQUE (ancestor, descendant)
);

-- Add index(es)
CREATE INDEX idx_attributes_hierarchy_ancestor
ON diagnosis.diagnosis_hierarchy(ancestor);

CREATE INDEX idx_attributes_hierarchy_descendant
ON diagnosis.diagnosis_hierarchy(descendant);

CREATE INDEX idx_attributes_hierarchy_depth
ON diagnosis.diagnosis_hierarchy(depth);