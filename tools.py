from Bio import Entrez
from dotenv import load_dotenv
import os
from langchain.tools import tool
from langchain_mistralai import ChatMistralAI
from typing_extensions import List, Optional

load_dotenv()

Entrez.email = os.getenv('ENTREZ_EMAIL')
Entrez.api_key = os.getenv('ENTREZ_API_KEY')

# 9 E-Utilities are available, the idea is to build a function for each 
# E-Utility that the agent can access via tool calling

@tool
def e_search(db: str, query_text: str):
    """Responds to a text query with the list of matching UIDs in a given database (for later use in ESummary, EFetch or ELink), along with the term translations of the query.

    Args:
    db: Entrez database name
    query_text: Query string in the format of term1[field1] OP term2[field2] ... , where OP is a boolean operator AND, OR or NOT. 
    """

    result = Entrez.read(Entrez.esearch(db=db,term=query_text))
    return result

@tool
def e_info(db: str):
    """Provides the number of records indexed in each field of a given database, the date of the last update of the database, and the available links from the database to other Entrez databases.

    Args:
    db: Entrez database name
    """

    result = Entrez.read(Entrez.einfo(db=db))
    return result

@tool
def e_post(db: str, id: List[str]):
    """Accepts a list of UIDs from a given database, stores the set on the History Server, and responds with a query key and web environment for the uploaded dataset.

    Args:
    db: Entrez database name
    id: list of IDs that will uploaded to the history server
    """
    result = Entrez.read(Entrez.epost(db=db,id=id))
    return result
    

@tool
def e_summary(db: str, id: List[str]):
    """Responds to a list of UIDs from a given database with the corresponding document summaries.

    Args:
    db: Entrez database name
    id: list of IDs for which summary is required
    """
    result = Entrez.read(Entrez.esummary(db=db,id=id))
    return result

@tool
def e_fetch(
    db: str,
    id: Optional[List[str]] = None,
    query_key: Optional[str] = None,
    web_env: Optional[str] = None,
    rettype: str = "abstract",
    retmode: str = "xml",
) -> dict:
    """Retrieve full records for a set of UIDs from a given Entrez database.

    Accepts either an explicit list of UIDs, or a reference to a result set
    already stored on the NCBI History server (query_key + web_env, as
    returned by e_search or e_post). Prefer the history-server form for
    result sets larger than ~200 IDs to avoid URL length limits.

    Args:
        db: Entrez database to fetch from (e.g. "pubmed").
        id: List of UIDs to fetch. Required if query_key/web_env are not given.
        query_key: History-server query key from a prior e_search or e_post call.
        web_env: History-server web environment from a prior e_search or e_post call.
        rettype: Record view to return (e.g. "abstract", "medline", "full").
        retmode: Format of the returned data ("xml" or "text").

    Returns:
        dict with either:
          records: list of parsed records (retmode="xml")
          raw: raw text body (retmode="text")
    """
    if not id and not (query_key and web_env):
        raise ValueError(
            "e_fetch requires either `id`, or both `query_key` and `web_env`."
        )

    params = {"db": db, "rettype": rettype, "retmode": retmode}
    if query_key and web_env:
        params["query_key"] = query_key
        params["webenv"] = web_env
    else:
        params["id"] = ",".join(id)

    handle = Entrez.efetch(**params)
    try:
        if retmode == "xml":
            parsed = Entrez.read(handle)
            records = parsed if isinstance(parsed, list) else [parsed]
            return {"records": records, "count": len(records)}
        else:
            return {"raw": handle.read()}
    finally:
        handle.close()

@tool
def e_link(
    dbfrom: str,
    db: str,
    id: Optional[List[str]] = None,
    query_key: Optional[str] = None,
    web_env: Optional[str] = None,
    linkname: Optional[str] = None,
    cmd: str = "neighbor",
) -> dict:
    """Find related records in another Entrez database, or an external resource,
    linked to a given set of UIDs.

    Common uses in this pipeline: pubmed -> pmc (find full text for an
    abstract), pubmed -> pubmed (find similar/cited-by articles for a
    relevance check), or pubmed -> mesh (cross-reference indexing terms).

    Accepts either an explicit list of UIDs, or a history-server reference
    (query_key + web_env) from a prior e_search or e_post call — same
    convention as e_fetch.

    Args:
        dbfrom: Database the input UIDs belong to (e.g. "pubmed").
        db: Database to find links in (e.g. "pmc"). Use the same value as
            dbfrom to find related records within the same database
            (e.g. "similar articles").
        id: List of UIDs to find links for. Required if query_key/web_env
            are not given.
        query_key: History-server query key from a prior e_search or e_post call.
        web_env: History-server web environment from a prior e_search or e_post call.
        linkname: Restrict to a specific link category (e.g. "pubmed_pubmed_citedin").
            If omitted, returns all link categories NCBI defines between dbfrom and db.
        cmd: elink mode. "neighbor" (default) returns linked UIDs grouped by
            category. "neighbor_history" posts the linked set to the History
            server instead of returning UIDs directly, for chaining into
            e_fetch on a large linked set.

    """
    if not id and not (query_key and web_env):
        raise ValueError(
            "e_link requires either `id`, or both `query_key` and `web_env`."
        )

    params = {"dbfrom": dbfrom, "db": db, "cmd": cmd}
    if linkname:
        params["linkname"] = linkname
    if query_key and web_env:
        params["query_key"] = query_key
        params["webenv"] = web_env
    else:
        params["id"] = id  # Entrez.elink accepts a list directly here

    handle = Entrez.elink(**params)
    try:
        parsed = Entrez.read(handle)
    finally:
        handle.close()

    if cmd == "neighbor_history":
        result_set = parsed[0]["LinkSetDbHistory"][0] if parsed[0].get("LinkSetDbHistory") else {}
        return {
            "web_env": result_set.get("WebEnv"),
            "query_key": result_set.get("QueryKey"),
        }

    links = {}
    for linkset in parsed[0].get("LinkSetDb", []):
        name = linkset["LinkName"]
        links[name] = [entry["Id"] for entry in linkset.get("Link", [])]
    return {"links": links}



model = ChatMistralAI(
    model="mistral-medium",
    temperature=0,
    api_key=os.getenv('MISTRAL_API_KEY')
)

tools = [e_fetch,e_info,e_link,e_post,e_search,e_summary]
tools_by_name = {tool.name: tool for tool in tools}


model_with_tools = model.bind_tools(tools)