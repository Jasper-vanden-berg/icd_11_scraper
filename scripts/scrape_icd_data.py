import logging
import re
import sys
import yaml
import asyncio
import aiohttp

import pandas as pd

from pathlib import Path
from collections import defaultdict

# Set up project root
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


# -----------------------------
# initiate the dictionaries we will use to save the data
# -----------------------------
class ScraperState:
    def __init__(self):
        self.diagnosis_table = {}
        self.diagnosis_hierarchy_table = {}
        self.diagnosis_synonyms_table = {}
        self.diagnosis_relationships_table = {}
        self.diagnosis_attributes_table = {}


# -----------------------------
# Retrieve the extension codes, keeping the exceptions (code/other and code/unspecified)
# -----------------------------
def url_splitter(url_list):
    if isinstance(url_list, str):
        url_list = [url_list]

    result = []
    for url in url_list:
        node_id = url.split("/")[-1] if url.split("/")[-1].isdigit() else "/".join(url.split("/")[-2:])
        if "mms" not in url and "entity" not in url:
            raise ValueError(f"Unsupported url found {url}. Only mms and entity type urls are supported")
        if node_id not in result:
            result.append(node_id)

    return result


# -----------------------------
# Replace pseudo-booleans with real booleans
# -----------------------------
def to_bool(x):
    if x is None:
        return None

    v = str(x).strip().lower()
    bool_true = {"true", "allowalways","allowedexceptfromsameblock"}
    bool_false = {"false", "notallowed"}

    if v not in bool_true and v not in bool_false:
        raise ValueError(f"Pseudo-boolean found for which no conversion has been defined {v}")

    return v in bool_true


# -----------------------------
# Recursively merge parent and child attributes and relationships
# -----------------------------
def merge_from_parent(parent, child):
    if not parent: return dict(child)
    r = dict(child)
    for k, pv in parent.items():
        cv = r.get(k)
        if cv is None: r[k] = pv
        elif isinstance(pv, dict) and isinstance(cv, dict): r[k] = merge_from_parent(pv, cv)
        elif isinstance(pv, list) and isinstance(cv, list): r[k] = list(dict.fromkeys(pv + cv))
    return r


# -----------------------------
# Async HTTP initializer for the API
# -----------------------------
async def get_token(api_settings, session):
    payload = {
        "client_id": api_settings["client_id"],
        "client_secret": api_settings["client_secret"],
        "scope": api_settings["scope"],
        "grant_type": api_settings["grant_type"],
    }

    async with session.post(api_settings["token_endpoint"], data=payload) as r:
        r.raise_for_status()
        data = await r.json()
        return data["access_token"]


# -----------------------------
# Async data fetcher
# -----------------------------
async def fetch_json(session, url, headers, semaphore):
    async with semaphore:
        try:
            async with session.get(url, headers=headers, timeout=10) as r:
                if r.status == 404:
                    return None
                r.raise_for_status()
                return await r.json()

        except Exception as e:
            logging.error(f"Request failed: {e}")
            return None


# -----------------------------
# Retrieve the official (main) and unofficial (index_term) children of the target node
# -----------------------------
def retrieve_children(data):
    main_child_data = url_splitter(data.get("child", []) or [])

    index_terms = data.get("indexTerm") or []
    index_term_data = url_splitter(
        x.get("foundationReference")
        for x in index_terms
        if x.get("foundationReference")
    )

    return [
        *main_child_data,
        *[x for x in index_term_data if x not in main_child_data]
    ]


# -----------------------------
# Retrieve the relationships (diagnosis to diagnosis) and attributes (diagnosis to non-diagnosis) data of a node
# -----------------------------
def retrieve_relationships(data,node_id,children):
    rel_keys = {"hasManifestation", "hasCausingCondition", "associatedWith"}
    attrs, rels =  {},{}

    #Here we retrieve relationship data, we save it in a dict per relationship type (Causes, ManifestsIn etc.)
    #For some reason, there is some self referencing in the icd-11 (Copper deficiency Has Manifestation Copper deficieny), so we have to build to ignore these
    for group in data.get("postcoordinationScale") or []:
        attr_type = group.get("axisName", "").rsplit("/", 1)[-1]
        entry = {
            "required": to_bool(group.get("requiredPostcoordination", None)),
            "allow_multiple": to_bool(group.get("allowMultipleValues", False)),
            "options": [
                    x for x in url_splitter(group.get("scaleEntity") or [])
                    if x not in children and x != node_id
                ]
        }
        if attr_type in rel_keys:
            rels[attr_type] = entry
        else:
            attrs[attr_type] = entry

    return attrs, rels

# -----------------------------
# Overarching function for retrieving all relevant data for a node
# -----------------------------
async def process_urls(session, urls, headers, node_id, semaphore):
    #There are 2 different icd_11 apis, we try the extensive one first (which is present for most official node_ids)
    #If it returns nothing, we fall back to the less extensive one (which is present for all nodes)
    data = await fetch_json(session, urls[0] + node_id, headers, semaphore)
    if not data:
        data = await fetch_json(session, urls[1] + node_id, headers, semaphore)

    title = data.get("title") or {}
    name = title.get("@value", "").replace("\t", " ")
    code = data.get("code")

    children = retrieve_children(data)
    attributes, relationships = retrieve_relationships(data,node_id,children)

    #Get the synonyms, we just extract the english ones for now
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
        "synonyms": synonyms,
    }


# -----------------------------
# ASYNC tree scraper, the main function that is being recursively executed. 
# -----------------------------
async def scrape_tree(session, urls, headers, node_id, state, base_codes,
                      semaphore, seen, lock, parent_id=None, diag_type="diagnosis"):

    diag_type = base_codes.get(node_id, diag_type)

    async with lock:
        if node_id in seen:
            return
        seen.add(node_id)

    diagnosis = await process_urls(session, urls, headers, node_id, semaphore)
    if not diagnosis:
        return
    
    state.diagnosis_table[node_id] = {
        "name": diagnosis["name"],
        "code": diagnosis["code"],
        "type": diag_type,
    }
    if diagnosis["children"]:
        state.diagnosis_hierarchy_table[node_id] = diagnosis["children"]
    if diagnosis["synonyms"]:
        state.diagnosis_synonyms_table[node_id] = diagnosis["synonyms"]
        
    parent_attrs = state.diagnosis_attributes_table.get(parent_id, {})
    parent_rels = state.diagnosis_relationships_table.get(parent_id, {})

    merged_attrs = merge_from_parent(parent_attrs, diagnosis["attributes"])
    merged_rels = merge_from_parent(parent_rels, diagnosis["relationships"])

    if merged_attrs:
        state.diagnosis_attributes_table[node_id] = merged_attrs
    if merged_rels and diag_type == "diagnosis":
        state.diagnosis_relationships_table[node_id] = merged_rels

    if len(state.diagnosis_table) % 1000 == 0:
        logging.info(f"Processed {len(state.diagnosis_table)} nodes out of roughly 75000")

    tasks = []
    for child_id in diagnosis["children"]:
        if child_id not in state.diagnosis_table:
            tasks.append(
                scrape_tree(
                    session, urls, headers,
                    child_id, state, base_codes,
                    semaphore, seen, lock, node_id, diag_type
                )
            )

    await asyncio.gather(*tasks)


def export_to_tsv(data, path, file_name):
    rows = []
    for k,v in data.items():
        if file_name == "diagnosis":
            columns = ["icd_11_id","name","code","type"]
            rows.append([k,v["name"],v["code"],v["type"]])
        elif file_name in ["hierarchy","synonyms"]:
            columns = ["icd_11_id","synonym"] if file_name == "synonyms" else ["parent_id","child_id"]
            rows.extend([k,x] for x in v)
        elif file_name in ["attributes","relationships"]:
            columns = ["icd_11_id","type","required","allow_multiple"]
            columns.append("to_diagnosis_id" if file_name=="relationships" else "attribute_id")
            for k2,v2 in v.items():
                row_split = [[k,k2,v2["required"],v2["allow_multiple"],x] for x in v2["options"]]
                rows.extend(row_split)

    file_name = file_name + ".tsv"
    Path(path).mkdir(parents=True, exist_ok=True)
    file_path = path / file_name
    df = pd.DataFrame(rows,columns=columns)
    df.to_csv(file_path,sep="\t",index=False)
# -----------------------------
# MAIN
# -----------------------------
async def async_main():
    config_path = Path(f"{project_root}/config.yml")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    api_settings = config["scraper"]["api_settings"]
    scrape_settings = config["scraper"]["scrape_settings"]
    icd_settings = config["scraper"]["icd"]

    urls = scrape_settings["urls"]
    main_ancestor_id = scrape_settings["main_ancestor_id"]

    base_codes = {v["id"]: k for k, v in icd_settings["base_codes"].items()}

    state = ScraperState()

    semaphore = asyncio.Semaphore(100)
    lock = asyncio.Lock()
    seen = set()

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = await get_token(api_settings, session)

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Language": "en",
            "API-Version": "v2",
        }

        await scrape_tree(
            session,
            urls,
            headers,
            main_ancestor_id,
            state,
            base_codes,
            semaphore,
            seen,
            lock,
        )

    out_dir = scrape_settings["out_dir"]
    out_path = Path(f"{project_root}/{out_dir}/raw/")
    export_to_tsv(state.diagnosis_table,out_path,"diagnosis")
    export_to_tsv(state.diagnosis_attributes_table, out_path,"attributes")
    export_to_tsv(state.diagnosis_hierarchy_table, out_path,"hierarchy")
    export_to_tsv(state.diagnosis_relationships_table, out_path,"relationships")
    export_to_tsv(state.diagnosis_synonyms_table,out_path,"synonyms")



if __name__ == "__main__":
    asyncio.run(async_main())