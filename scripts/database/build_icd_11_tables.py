import yaml
import logging
from pathlib import Path
import pandas as pd
import psycopg2
import sys
from psycopg2.extras import execute_batch,execute_values

def load_config(config_path):
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config


def get_connection(db_config):
    return psycopg2.connect(
        host=db_config["host"],
        port=db_config["port"],
        dbname=db_config["dbname"],
        user=db_config["user"],
        password=db_config["password"],
    )


def run_sql_file(conn, path: Path):
    with open(path, "r") as f:
        sql = f.read()

    with conn.cursor() as cur:
        cur.execute(sql)

    conn.commit()


def load_schemas(conn, schema_root: Path):
    prio = ["attributes/attributes.sql","diagnosis/diagnosis.sql"]
    for sql_file in prio:
            sql_table = schema_root / sql_file
            logging.info(f"Applying schema1: {sql_table}")
            run_sql_file(conn, sql_table)

    for sql_file in schema_root.rglob("*.sql"):
        if any(sql_file.match(schema_root / p) for p in prio):
            continue
        logging.info(f"Applying schema2: {sql_file}")
        run_sql_file(conn, sql_file)


def load_tsv(path: Path):
    if not path.exists():
        logging.warning(f"Missing file: {path}")
        return None

    return pd.read_csv(path, sep="\t")

def build_mapping(conn, table, key_col):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT {key_col}, id
            FROM {table}
        """)
        return dict(cur.fetchall())


def insert_dataframe(conn, table_name: str, df: pd.DataFrame):
    if df is None or df.empty:
        logging.warning(f"No data for {table_name}")
        return 0

    cols = list(df.columns)
    values = [tuple(x.item() if hasattr(x, "item") else x for x in row)
          for row in df.to_numpy()]

    columns = ",".join(cols)

    sql = f"""
        INSERT INTO {table_name} ({columns})
        VALUES %s
        RETURNING 1
    """

    with conn.cursor() as cur:
        execute_values(cur, sql, values)
        inserted = cur.fetchall()

    conn.commit()

    inserted_count = len(inserted)
    logging.info(f"{table_name}: inserted {inserted_count}/{len(df)} rows")

def map_ids(df,diag_map,attr_map):
    df = df.copy()

    mapping_spec = {
        "diagnosis" : ["diagnosis_ancestor","diagnosis_descendant","from_diagnosis_id","to_diagnosis_id","diagnosis_id"],
        "attributes": ["attributes_ancestor","attributes_descendant","attribute_id"]
    }

    rename_map = {}
    for col in df.columns:

        if col in mapping_spec["diagnosis"]:
            mapped = df[col].astype(str).map(diag_map)

            if mapped.isna().any():
                missing = df.loc[mapped.isna(), col].unique()
                raise ValueError(
                    f"Failed to map diagnosis IDs in column '{col}'. "
                    f"Missing values: {missing[:20]}"
                )
            
            df[col] = df[col].astype(str).map(diag_map).astype("Int64")
        elif col in mapping_spec["attributes"]:
            df[col] = df[col].astype(str).map(attr_map).astype("Int64")

    for col in df.columns:
        if col.endswith("_ancestor"):
            rename_map[col] = "ancestor"
        elif col.endswith("_descendant"):
            rename_map[col] = "descendant"

    df = df.rename(columns=rename_map)
    print(df)
    return df




def main():
    logging.basicConfig(level=logging.INFO)

    config = load_config("config.yml")
    db_config = config["database"]["postgres"]
    input_dir = Path(config["database"]["input_dir"])

    conn = get_connection(db_config)

    #1. Load the database Schemas
    schema_root = Path("schema")
    load_schemas(conn, schema_root)

    #2. Create the diagnosis and attributes tables first (so we can use their generated ids)
    df_diag = load_tsv(input_dir / "diagnosis/diagnosis.tsv")
    df_attr = load_tsv(input_dir / "attributes/attributes.tsv")
    insert_dataframe(conn, "diagnosis.diagnosis", df_diag)
    insert_dataframe(conn, "diagnosis.attributes", df_attr)

    #3. Get the mapping of the ids
    diag_map = build_mapping(conn, "diagnosis.diagnosis", "icd_11_id")
    attr_map = build_mapping(conn, "diagnosis.attributes", "icd_11_id")


    table_mapping = {
        "diagnosis.diagnosis_hierarchy" : "diagnosis/closure.tsv",
        "diagnosis.diagnosis_synonyms" : "diagnosis/synonyms.tsv",
        "diagnosis.diagnosis_relationships" : "diagnosis/relationships.tsv",
        "diagnosis.attributes_hierarchy" : "attributes/closure.tsv",
        "diagnosis.diagnosis_attributes": "attributes/diagnosis_attributes.tsv"
    }

    for target_table, target_file in table_mapping.items():
        print(f"processing table {target_table}")
        df = load_tsv(Path(input_dir / target_file))
        df = map_ids(df,diag_map,attr_map)
        insert_dataframe(conn,target_table,df)

    conn.close()


if __name__ == "__main__":
    main()