import logging
import re
import requests
import sys
import yaml

import pandas as pd

from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock



def fetch_child(args):
    return scrape_tree(*args)

# Set up project root
project_root = Path(__file__).resolve().parents[2]
sys.path.append(str(project_root))

# Set up api request session
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=100,
    pool_maxsize=100,
    max_retries=2,
    pool_block=True,   # important change
)
session.mount("http://", adapter)
session.mount("https://", adapter)

seen_lock = Lock()
seen = set()

def claim(node_id):
    with seen_lock:
        if node_id in seen:
            return False
        seen.add(node_id)
        return True

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

class ScraperState:
    def __init__(self):
        self.diagnosis_table = {}
        self.diagnosis_relationships_table = {}

        self.attributes_table = {}
        self.attributes_hierarchy_table = {}
        self.attributes_hierarchy_closure_table = {}
        self.diagnosis_attributes_table = {}





# Requests OAuth access token using client credentials flow and returns bearer token
def get_token(api_settings):
    payload = {
        "client_id": api_settings["client_id"],
        "client_secret": api_settings["client_secret"],
        "scope": api_settings["scope"],
        "grant_type": api_settings["grant_type"]
    }
    r = session.post(api_settings["token_endpoint"], data=payload)
    r.raise_for_status()
    return r.json()["access_token"]


# Performs a GET request with timeout, returns parsed JSON, and handles 404 and request errors safely
def fetch(url, headers):
    try:
        r = session.get(url, headers=headers, timeout=10)

        if r.status_code == 404:
            return None  # or {}

        r.raise_for_status()
        return r.json()

    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {e}")
        return None


#Returns the part of the url containing the diagnosis code (and any optional headers after it)
def url_splitter(url_list):
    #If processing only 1 url, just list it so it can go through the loop
    if type(url_list) == str:
        url_list = [url_list]

    result = []

    for url in url_list:
        #Split the url, take either just the last part (the diagnosis code) or the last 2 parts (diagnosis/other)
        node_id = url.split("/")[-1] if url.split("/")[-1].isdigit() else "/".join(url.split("/")[-2:])
        #Save the diagnosis code and save what type of url it came from (mms and entity urls contain different info)
        if "mms" in url:
            result.append((node_id, "mms"))
        elif "entity" in url:
            result.append((node_id, "entity"))
    return result


#Transforms pseudo boolean valeus (True/False as str ; allowed/notallowed) to true booleans
def to_bool(x):
    if x is None:
        return None
    #Just a sanity check to confirm we are catching every possible pseudo boolean value
    sanity_check = str(x).strip().lower() in ["true","allowalways","false","notallowed","allowedexceptfromsameblock"]
    if not sanity_check:
        logging.error(f"unallowed value found {x}")
        sys.exit()

    return str(x).strip().lower() in ["true","allowalways","allowedexceptfromsameblock"]
    

#The main processing function, responsible for extracting the required data of each diagnosis
#In short, this function extracts:
#   General data (name, icd-11 Code)
#   Children (official and unofficial)
#   Synonyms (english)
#   Relationships (between diagnoses)
#   Attributes (of diagnoses) 
def process_urls(urls, headers, node_id, url_type):
    #Get the correct url based on whether we are processing an official child or an index term/unofficial subtype, since they are stored differently in the API
    url = urls[0] if url_type == "mms" else urls[1]
    data = fetch(url + node_id, headers)

    #Retrieve surface data like name and code
    title = data.get("title") or {}
    name = title.get("@value", "").replace("\t", " ")
    code = data.get("code")
    
    #Initialize variables for storing data
    attributes = defaultdict(dict)
    synonyms = []

    #Retrieve official children in the "child" field
    children = []

    main_child_data = url_splitter(data.get("child", []) or [])

    index_terms = data.get("indexTerm") or []
    index_term_data = url_splitter(
        x.get("foundationReference")
        for x in index_terms
        if x.get("foundationReference")
    )

    for x,y in main_child_data:
        if claim(x):
            children.append((x,y))

    for x,y in index_term_data:
        if x not in main_child_data:
            if claim(x):
                children.append((x,y))
    #Get postcoordination attributes if they exist, this can be either relationships (diagnosis A manifests in diagnosis B) or attributes (diagnosis A has severity x,y or z)
    for group in data.get("postcoordinationScale") or []:
        attr_type = group.get("axisName", "").rsplit("/", 1)[-1]
        entry = attributes[attr_type]
        entry["required"] = to_bool(group.get("requiredPostcoordination", None))
        entry["allow_multiple"] = to_bool(group.get("allowMultipleValues", False))
        scale = group.get("scaleEntity") or []
        entry["options"] = [x[0] for x in url_splitter(scale)]


    #Catch and store english synonyms of diagnoses
    for synonym in data.get("synonym", []):
        label = synonym.get("label") or {}
        if label.get("@language") == "en":
            synonyms.append(label.get("@value", ""))

    logging.debug(f"Processed node {node_id}: {name} \
                    with {len(children)} children, \
                    {len(synonyms)} synonyms, \
                    and {len(attributes)} attributes/relationships"
                )  
    return {
        "name": name,
        "code": code,
        "children": children,
        "attributes": attributes,
        "synonyms": synonyms
    }


#Ensure children inherits traits from their parents
#As an example, the diagnosis "Alzheimer due to dementia" has a mandatory relationship with "Alzheimers Disease"
#Therefor, any child diagnosis of "Alzheimer due to dementia" should also have this relationship.
def merge_from_parent(parent, child):
    if not parent:
        return child

    for k, pv in parent.items():
        cv = child.get(k)

        if cv is None:
            child[k] = pv

        elif isinstance(pv, bool) and isinstance(cv, bool):
            child[k] = cv or pv

        elif isinstance(pv, list) and isinstance(cv, list):
            # preserves order, removes duplicates
            dseen = set()
            merged = []
            for x in cv + pv:
                if x not in dseen:
                    dseen.add(x)
                    merged.append(x)
            child[k] = merged

        elif isinstance(pv, dict) and isinstance(cv, dict):
            child[k] = merge_from_parent(pv, cv)

        else:
            logging.error(
                "type conflict for %s: parent=%s child=%s",
                k, type(pv), type(cv)
            )
            raise TypeError(f"conflicting types for key {k}")

    return child


#Splits the attribute data into relationships (links between diagnoses) and attributes (elements of a diagnosis)
def split_attributes(attributes):
    #The 3 relationships we want
    rel_keys = {"hasManifestation", "hasCausingCondition", "associatedWith"}
    rels = {}
    attrs = {}

    for k, v in attributes.items():
        if k in rel_keys:
            rels[k] = v
        else:
            attrs[k] = v
    logging.debug(f"Split attributes into {len(attrs)} attributes and {len(rels)} relationships")
    return attrs, rels


#The main recursive function
#Processes a target url and recursively processes all its children
def scrape_tree(urls, headers, node_id,state, base_codes, url_type="mms",parent_id=None,diag_type="diagnosis"):
    logging.debug(f"Processing node {node_id} with parent {parent_id}")
    diag_type = base_codes.get(node_id, diag_type)
    #get diagnosis data for the current node
    diagnosis = process_urls(
        urls,headers,node_id,url_type
    )
    #Children should inherit the postcoordination attributes of their parents, this enforces that rule for when the child data forgot to mention it explicitly.
    parent_attrs, parent_rels = state.diagnosis_attributes_table.get(parent_id, {}), state.diagnosis_relationships_table.get(parent_id, {})
    child_attrs, child_rels   = split_attributes(diagnosis["attributes"])
    merged_attrs, merged_rels = merge_from_parent(parent_attrs, child_attrs), merge_from_parent(parent_rels, child_rels)

    #store diagnosis data
    state.diagnosis_table[node_id] = [
        diagnosis["name"],
        diagnosis["code"],
        diag_type,
        [child_id for child_id,_url_type in diagnosis["children"]],
        diagnosis["synonyms"]
    ]
 
    if merged_attrs:
        state.diagnosis_attributes_table[node_id] = merged_attrs
    if merged_rels and diag_type == "diagnosis":
        state.diagnosis_relationships_table[node_id] = merged_rels
    if len(state.diagnosis_table.keys()) % 100 == 0:
        logging.info(f"Processed {len(state.diagnosis_table.keys())} diagnoses so far, currently at type {diag_type}, node {node_id}")
    if len(state.diagnosis_table.keys()) >= 2000:
        return
        

    #Recursively scrape children
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for child_id, url_type in diagnosis["children"]:
            #The main node_id (455013390) is a special case, in that it is the only url that is an entity url that produces mms urls
            if node_id == "455013390":
                url_type = "mms"
            if child_id not in state.diagnosis_table.keys():
                futures.append(executor.submit(scrape_tree, urls, headers, child_id, state, base_codes, url_type, node_id, diag_type))
        for future in futures:
            future.result()
                #scrape_tree(urls, headers, child_id, state, base_codes, url_type, node_id, diag_type)



#This function explodes any lists we have before we write to tsv
def expand_edges(store,label):
    return [
        (icd_id, attr, v["required"], v["allow_multiple"], option)
        for icd_id, attrs in store.items()
        for attr, v in attrs.items()
        for option in v.get("options", [])
    ]

#this function turns our basic hierarchy into a proper closure table, ready for creation
def build_closure_table(diagnosis_table,state):
    logging.debug("Building closure table from diagnosis_table")

    rows = []

    # helper DFS
    def dfs(parent_id, node_id, depth):
        rows.append({
            "parent_id": parent_id,
            "child_id": node_id,
            "depth": depth
        })

        children = diagnosis_table.get(node_id, [None, None, None, [], None])[3] or []
        for child_id in children:
            dfs(parent_id, child_id, depth + 1)

    # find roots (never appear as children)
    all_children = {
        c
        for v in diagnosis_table.values()
        for c in (v[3] or [])
    }

    roots = [n for n in diagnosis_table.keys() if n not in all_children]

    # build closure starting from each root
    for root in roots:
        dfs(root, root, 0)

    logging.debug(
        f"Built closure table with {len(rows)} rows from {len(diagnosis_table)} nodes"
    )

    return rows


def build_relationships_table(relationships_store, base_codes):
    logging.debug("Building relationships table from relationships_store")

    rows = []

    for icd_id, rels in relationships_store.items():
        for rel, v in rels.items():
            for option in v.get("options", []):
                if option in base_codes.keys():
                    continue
                rows.append({
                    "from_diagnosis_id": icd_id,
                    "to_diagnosis_id": option,
                    "relationship": rel,
                    "required": v.get("required"),
                    "allow_multiple": v.get("allow_multiple"),
                })

    logging.debug(
        f"Built relationships table with {len(rows)} rows from {len(relationships_store)} nodes"
    )

    return rows


def get_all_children(node_id,attributes_hierarchy_closure_table):
    return [
        row["child_id"]
        for row in attributes_hierarchy_closure_table
        if row["parent_id"] == node_id
    ]


def build_diagnosis_attributes_table(attributes_store):
    logging.debug("Building diagnosis attributes table from attributes_store")
    

    rows = []
    for icd_id, attrs in attributes_store.items():
        for attr, v in attrs.items():
            for option in v.get("options", []):
                rows.append({
                    "icd_id": icd_id,
                    "attribute": attr,
                    "required": v.get("required"),
                    "allow_multiple": v.get("allow_multiple"),
                    "option": option
                })



def create_diagnoses_tables(state, base_codes):
    logging.debug("Creating diagnoses tables")
    true_diagnoses = {
        node_id: v
        for node_id, v in state.diagnosis_table.items()
        if v[2] == "diagnosis"
    }
    diagnosis_table = {
        k: {
            "name": v[0],
            "code": v[1]
        }
        for k, v in true_diagnoses.items()
    }
    diagnosis_hierarchy_closure_table = build_closure_table(true_diagnoses,state)
    diagnosis_synonyms_table = {
        k: {
            "synonyms": v[4],
            "lang": "en"
            
        }
        for k, v in true_diagnoses.items()
        if v[4]
    }
    diagnosis_relationships_table = build_relationships_table(state.diagnosis_relationships_table,base_codes)

    return {
        "diagnosis": diagnosis_table,
        "closure": diagnosis_hierarchy_closure_table,
        "synonyms": diagnosis_synonyms_table,
        "relationships": diagnosis_relationships_table
    }

def create_attributes_tables(state,base_codes,extension_mappings):
    logging.debug("Creating attributes tables")
    true_attributes = {
        node_id: v
        for node_id, v in state.diagnosis_table.items()
        if v[2] == "extension_codes"
    }
    attributes_table = {
        k: {
            "name": v[0],
            "code": v[1]
        }
        for k, v in true_attributes.items()
    }
    attributes_hierarchy_closure_table = build_closure_table(true_attributes,state)
    diagnisis_attributes_table = build_diagnosis_attributes_table(state.diagnosis_attributes_table)

    return {
        "attributes": attributes_table,
        "closure": attributes_hierarchy_closure_table,
        "diagnosis_attributes": diagnisis_attributes_table
    }


def export_to_tsv(all_tables, out_path):
    logging.debug(f"Exporting tables to tsv at {out_path}")

    out_path.mkdir(parents=True, exist_ok=True)

    for group_name, tables in all_tables.items():

        group_path = out_path / group_name
        group_path.mkdir(parents=True, exist_ok=True)

        for table_name, data in tables.items():

            file_path = group_path / f"{table_name}.tsv"

            # ✅ HANDLE EMPTY TABLES
            if data is None:
                logging.warning(f"Skipping empty table: {group_name}.{table_name}")
                pd.DataFrame().to_csv(file_path, sep="\t", index=False)
                continue

            # list of dicts (closure, relationships)
            if isinstance(data, list):
                df = pd.DataFrame(data)

            # dict of dicts (diagnosis, attributes, synonyms)
            elif isinstance(data, dict):
                df = pd.DataFrame([
                    {"node_id": k, **v}
                    for k, v in data.items()
                ])

            else:
                raise ValueError(
                    f"Unsupported format for {group_name}.{table_name}: {type(data)}"
                )
            logging.info(f"Exporting {len(df)} rows to {file_path}")
            df.to_csv(file_path, sep="\t", index=False)


def main():
    config_path = Path(f"{project_root}/config.yml")
    with open(config_path, "r") as f:
        logging.debug(f"Loaded configuration from {config_path}")
        config = yaml.safe_load(f)
        api_settings = config["scraper"]["api_settings"]
        scrape_settings = config["scraper"]["scrape_settings"]
        icd_settings = config["scraper"]["icd"]

    out_dir = scrape_settings["out_dir"]
    out_path = Path(f"{project_root}/{out_dir}")

    main_ancestor_id = scrape_settings["main_ancestor_id"]
    main_ancestor_child_type = scrape_settings["main_ancestor_child_type"]
    urls = scrape_settings["urls"]
    base_codes = icd_settings["base_codes"]
    base_codes = {v["id"]: k for k, v in base_codes.items()}
    extension_mappings = icd_settings["extension_mappings"]
    

    token = get_token(api_settings)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "en",
        "API-Version": "v2"
    }
    session.headers.update(headers)
    state = ScraperState()

    scrape_tree(urls, headers,main_ancestor_id,state, base_codes,  main_ancestor_child_type)
    all_tables = {
        "diagnosis": create_diagnoses_tables(state, base_codes),
        "attributes": create_attributes_tables(state, base_codes, extension_mappings)
    }
    export_to_tsv(all_tables, out_path)

if __name__ == "__main__":
    main()
