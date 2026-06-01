import yaml
import logging
from pathlib import Path
import pandas as pd
import psycopg2
from psycopg2.extras import execute_batch

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


def load_tsv(path: Path):
    if not path.exists():
        logging.warning(f"Missing file: {path}")
        return None

    return pd.read_csv(path, sep="\t")


def main():
    logging.basicConfig(level=logging.INFO)

    config = load_config("config.yml")

    db_config = config["database"]["postgres"]
    input_dir = Path(config["database"]["input_dir"])

    conn = get_connection(db_config)

    # map TSV files → postgres tables
    table_map = {
        "diagnosis/diagnosis.tsv": "diagnosis",
        "diagnosis/closure.tsv": "diagnosis_closure",
        "diagnosis/synonyms.tsv": "diagnosis_synonyms",
        "diagnosis/relationships.tsv": "diagnosis_relationships",
        "attributes/attributes.tsv": "attributes",
        "attributes/closure.tsv": "attributes_closure",
        "attributes/diagnosis_attributes.tsv": "diagnosis_attributes",
    }

    for file_rel, table_name in table_map.items():
        path = input_dir / file_rel

        df = load_tsv(path)
        insert_dataframe(conn, table_name, df)

    conn.close()


if __name__ == "__main__":
    main()