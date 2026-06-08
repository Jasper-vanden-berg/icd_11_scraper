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
        rows = cur.fetchall()

    return {
        str(k).strip(): v
        for k, v in rows
    }


def insert_dataframe(conn, table_name: str, df: pd.DataFrame, chunk_size: int = 5000):

    if df is None or df.empty:
        logging.warning(f"No data for {table_name}")
        return 0

    df = df.where(pd.notnull(df), None)

    cols = list(df.columns)
    columns = ",".join(cols)

    sql = f"INSERT INTO {table_name} ({columns}) VALUES %s"

    total_expected = len(df)
    total_sent = 0

    try:
        with conn.cursor() as cur:

            for start in range(0, total_expected, chunk_size):

                chunk = df.iloc[start:start + chunk_size]

                values = [
                    tuple(x.item() if hasattr(x, "item") else x for x in row)
                    for row in chunk.to_numpy()
                ]

                execute_values(cur, sql, values)

                total_sent += len(values)

        conn.commit()

        # IMPORTANT: trust what YOU sent, not rowcount
        if total_sent != total_expected:
            raise RuntimeError(
                f"Insert mismatch (internal counting): "
                f"expected {total_expected}, sent {total_sent}"
            )

        logging.info(f"{table_name}: inserted {total_sent}/{total_expected}")
        return total_sent

    except Exception as e:
        conn.rollback()
        raise RuntimeError(f"Insert failed for {table_name}") from e


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
    df_diag = load_tsv(input_dir / "clean/diagnosis.tsv")
    df_attr = load_tsv(input_dir / "clean/attributes.tsv")
    insert_dataframe(conn, "diagnosis.diagnosis", df_diag)
    insert_dataframe(conn, "diagnosis.attributes", df_attr)

    #3. Get the mapping of the ids
    diag_map = build_mapping(conn, "diagnosis.diagnosis", "icd_11_id")
    attr_map = build_mapping(conn, "diagnosis.attributes", "icd_11_id")


    table_mapping = {
        "diagnosis.diagnosis_hierarchy" : "diagnosis_hierarchy",
        "diagnosis.diagnosis_synonyms" : "synonym",
        "diagnosis.diagnosis_relationships" : "relationships",
        "diagnosis.attributes_hierarchy" : "attributes_hierarchy",
        "diagnosis.diagnosis_attributes": "diagnosis_attributes"
    }

    for target_table, target_file in table_mapping.items():
        file = Path(input_dir) / "clean" / f"{target_file}.tsv"
        print(file)
        df = load_tsv(file)
        if target_file == "diagnosis_hierarchy":
            df["ancestor_id"] = df["ancestor_id"].astype(str).map(diag_map).astype("Int64")
            df["descendant_id"] = df["descendant_id"].astype(str).map(diag_map).astype("Int64")
        elif target_file == "synonym":
            df["diagnosis_id"] = df["diagnosis_id"].astype(str).map(diag_map).astype("Int64")
        elif target_file == "relationships":
            df["from_diagnosis_id"] = df["from_diagnosis_id"].astype(str).map(diag_map).astype("Int64")
            df["to_diagnosis_id"] = df["to_diagnosis_id"].astype(str).map(diag_map).astype("Int64")
        elif target_file == "attributes_hierarchy":
            df["ancestor_id"] = df["ancestor_id"].astype(str).map(attr_map).astype("Int64")
            df["descendant_id"] = df["descendant_id"].astype(str).map(attr_map).astype("Int64")
        elif target_file == "diagnosis_attributes":
            df["diagnosis_id"] = df["diagnosis_id"].astype(str).map(diag_map).astype("Int64")
            df["attribute_id"] = df["attribute_id"].astype(str).map(attr_map).astype("Int64")
        insert_dataframe(conn,target_table,df)

    conn.close()


if __name__ == "__main__":
    main()