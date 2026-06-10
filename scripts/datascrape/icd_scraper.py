import asyncio
import logging
import sys
import yaml
from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path

import aiohttp



# Set up project root
project_root = Path(__file__).resolve().parents[2]
sys.path.append(str(project_root))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


@dataclass
class ScrapedData:
    general: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)
    hierarchy: dict = field(default_factory=dict)
    relationships: dict = field(default_factory=dict)


def url_splitter(url_list):
    if isinstance(url_list, str):
        url_list = [url_list]

    results = []
    for url in url_list:
        if not ("mms" in url or "entity" in url):
            continue

        parts = url.split("/")
        node = parts[-1] if parts[-1].isdigit() else "/".join(parts[-2:])
        if node not in results:
            results.append(node)

    return results


def to_bool(x):
    if x is None:
        return None
    
    target = str(x).strip().lower()
    true_bools = ["true","allowalways","allowedexceptfromsameblock"]
    false_bools = ["false","notallowed"]
    if target not in true_bools and target not in false_bools:
        raise ValueError("Now pseudo boolean value found without a known conversion")

    return target in true_bools


def split_attributes(attr_rel):
    #The 3 relationships we want
    rel_values = ["hasManifestation", "hasCausingCondition", "associatedWith"]
    rels = []
    attrs = []

    for sublist in attr_rel:
        if sublist[1] in rel_values:
            rels.append(sublist)
        else:
            rels.append(attrs)

    logging.debug(f"Split attributes into {len(attrs)} attributes and {len(rels)} relationships")
    return attrs, rels


def process_urls(urls, headers, node_id, url_type):
    #Get the correct url based on whether we are processing an official child or an index term/unofficial subtype, since they are stored differently in the API
    try:
        data = fetch(urls[0] + node_id, headers)
    except:
        data = fetch(urls[1] + node_id, headers)

    #Retrieve surface data like name and code
    title = data.get("title") or {}
    name = title.get("@value", "").replace("\t", " ")
    code = data.get("code")
    
    #Initialize variables for storing data

    #Retrieve official children in the "child" field
    main = url_splitter(data.get("child") or [])
    index = url_splitter(t["foundationReference"] for t in (data.get("indexTerm") or []) if t.get("foundationReference"))
    children = list(dict.fromkeys(main + index))


    attr_rel = []
    for group in data.get("postcoordinationScale") or []:
        entry = group.get("axisName", "").rsplit("/", 1)[-1]
        required = to_bool(group.get("requiredPostcoordination"))
        allow_multiple = to_bool(group.get("allowMultipleValues"))
        nodes = url_splitter(group.get("scaleEntity") or [])
        options = [node_id for node_id in nodes if node_id not in children]
        attr_rel.append([[entry,required,allow_multiple,option] for option in options])

    attributes, relationships = split_attributes(attr_rel)

    synonyms = []
    for synonym in data.get("synonym", []):
        label = synonym.get("label") or {}
        if label.get("@language") == "en":
            synonyms.append(label.get("@value", ""))  

      
    return {
        "name": name,
        "code": code,
        "children": children,
        "attributes": attributes,
        "relationships":relationships,
        "synonyms": synonyms
    }


def load_config(file_path,part):
    with open(file_path, "r") as f:
        logging.debug(f"Loaded configuration from {file_path}")
        config = yaml.safe_load(f)
        config_part = config[part]
        return config_part


def main():
    config_path = Path(f"{project_root}/config.yml")
    config_dict = load_config(config_path,"scraper")




if __name__ == "__main__":
    main()