from Bio import Entrez
from Bio.Entrez.Parser import CorruptedXMLError, NotXMLError, ValidationError
import os
import re
from urllib.error import HTTPError, URLError
from langchain.tools import tool

from typing_extensions import List, Literal, Optional

Entrez.email = os.getenv('ENTREZ_EMAIL')
Entrez.api_key = os.getenv('ENTREZ_API_KEY')

# NCBI hard limits / recommendations
ESEARCH_MAX_RETMAX = 10000
ESUMMARY_MAX_RETMAX = 10000
MAX_IDS_PER_REQUEST = 200  # beyond this, use e_post + history server
ENTREZ_DATE_RE = re.compile(r"^\d{4}(/\d{2}(/\d{2})?)?$")


def _to_builtin(obj):
    """Convert Bio.Entrez parser elements into plain Python types so the
    result serialises cleanly into a ToolMessage."""
    if isinstance(obj, dict):
        return {str(k): _to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_builtin(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, str):
        return str(obj)
    return obj


def _entrez_request(func, **params):
    """Call an Entrez utility, parse the XML response and always close the handle."""
    handle = func(**{k: v for k, v in params.items() if v is not None})
    try:
        return _to_builtin(Entrez.read(handle))
    finally:
        handle.close()


def _error(tool_name: str, exc: Exception) -> dict:
    """Turn an exception into an error payload the model can read and act on."""
    if isinstance(exc, HTTPError):
        hint = "Check the database name and parameter values."
        if exc.code == 429:
            hint = "Rate limited by NCBI; wait briefly and retry."
        elif exc.code >= 500:
            hint = "NCBI server error; retry later or reduce the request size."
        message = f"HTTP {exc.code} from NCBI: {exc.reason}. {hint}"
    elif isinstance(exc, URLError):
        message = f"Could not reach NCBI: {exc.reason}. Retry later."
    elif isinstance(exc, RuntimeError):
        # Entrez.read raises RuntimeError when NCBI returns an <ERROR> element
        message = f"NCBI rejected the request: {exc}"
    elif isinstance(exc, (ValidationError, CorruptedXMLError, NotXMLError)):
        message = f"Could not parse the NCBI response: {exc}"
    elif isinstance(exc, ValueError):
        message = f"Invalid arguments: {exc}"
    else:
        message = f"Unexpected {type(exc).__name__}: {exc}"
    return {"error": message, "tool": tool_name}


def _clean_ids(ids: Optional[List[str]]) -> List[str]:
    """Strip whitespace, drop empties and de-duplicate while preserving order."""
    if not ids:
        return []
    return list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip()))


@tool(parse_docstring=True)
def e_search(
    db: str,
    term: str,
    retmax: int = 20,
    retstart: int = 0,
    sort: Optional[str] = None,
    field: Optional[str] = None,
    datetype: Optional[Literal["pdat", "edat", "mdat"]] = None,
    mindate: Optional[str] = None,
    maxdate: Optional[str] = None,
    reldate: Optional[int] = None,
    use_history: bool = False,
) -> dict:
    """Search an Entrez database with a text query and return the matching UIDs.

    Use this as the first step to find records; pass the returned UIDs (or the
    history-server query_key/web_env) to e_summary, e_fetch or e_link. The
    response includes NCBI's query translation, which shows how the query was
    interpreted and helps diagnose zero-result searches.

    Args:
        db: Entrez database to search, e.g. "pubmed", "pmc", "gene", "protein", "nuccore", "mesh". Use e_info with no db to list all databases.
        term: Query in Entrez syntax, e.g. "BRCA1[Title/Abstract] AND humans[MeSH Terms]" (pubmed), "BRCA1[Gene Name] AND human[Organism]" (gene) or "asthma[MeSH Terms] AND 2020:2024[pdat]". Combine terms with AND, OR, NOT. Use e_info on the db to see valid [field] tags.
        retmax: Maximum number of UIDs to return (1-10000). Keep small unless many IDs are genuinely needed.
        retstart: Index of the first UID to return, for paging through results.
        sort: Database-specific sort order, e.g. for pubmed "relevance", "pub_date", "Author", "JournalName". Omit for the default order.
        field: Restrict the whole query to a single search field, e.g. "title". Equivalent to appending [field] to every term.
        datetype: Date field used by mindate/maxdate/reldate: "pdat" (publication date), "edat" (Entrez date) or "mdat" (modification date).
        mindate: Start of the date range as YYYY, YYYY/MM or YYYY/MM/DD. Must be given together with maxdate.
        maxdate: End of the date range as YYYY, YYYY/MM or YYYY/MM/DD. Must be given together with mindate.
        reldate: Only return records whose datetype date is within this many days of today.
        use_history: If true, store the full result set on the NCBI history server and return query_key and web_env for use with e_fetch, e_summary or e_link on large result sets.

    Returns:
        dict with count (total matches), retstart, retmax, ids, query_translation, and optionally query_key/web_env, warnings, and errors. On failure, a dict with an "error" key.
    """
    try:
        if not db.strip():
            raise ValueError("`db` must be a non-empty database name.")
        if not term.strip():
            raise ValueError("`term` must be a non-empty query string.")
        if not 1 <= retmax <= ESEARCH_MAX_RETMAX:
            raise ValueError(f"`retmax` must be between 1 and {ESEARCH_MAX_RETMAX}.")
        if retstart < 0:
            raise ValueError("`retstart` must be >= 0.")
        if bool(mindate) != bool(maxdate):
            raise ValueError("`mindate` and `maxdate` must be provided together.")
        for name, value in (("mindate", mindate), ("maxdate", maxdate)):
            if value and not ENTREZ_DATE_RE.match(value):
                raise ValueError(f"`{name}` must be YYYY, YYYY/MM or YYYY/MM/DD, got {value!r}.")
        if reldate is not None and reldate <= 0:
            raise ValueError("`reldate` must be a positive number of days.")
        if (mindate or reldate) and not datetype:
            datetype = "pdat"

        result = _entrez_request(
            Entrez.esearch,
            db=db.strip(),
            term=term,
            retmax=retmax,
            retstart=retstart,
            sort=sort,
            field=field,
            datetype=datetype,
            mindate=mindate,
            maxdate=maxdate,
            reldate=reldate,
            usehistory="y" if use_history else None,
        )
    except Exception as exc:
        return _error("e_search", exc)

    response = {
        "count": int(result.get("Count", 0)),
        "retstart": int(result.get("RetStart", retstart)),
        "retmax": int(result.get("RetMax", 0)),
        "ids": result.get("IdList", []),
        "query_translation": result.get("QueryTranslation", ""),
    }
    if use_history:
        response["query_key"] = result.get("QueryKey")
        response["web_env"] = result.get("WebEnv")
    # e.g. {"FieldNotFound": ["Gene Name"]} when a [field] tag is invalid for this db
    for key, out_key in (("WarningList", "warnings"), ("ErrorList", "errors")):
        issues = {k: v for k, v in (result.get(key) or {}).items() if v}
        if issues:
            response[out_key] = issues
    return response


@tool(parse_docstring=True)
def e_info(db: Optional[str] = None) -> dict:
    """Describe the Entrez databases, or the search fields and links of one database.

    Call with no db to list every available Entrez database name. Call with a
    db to learn which [field] tags can be used in e_search queries and which
    link names can be used with e_link.

    Args:
        db: Entrez database to describe, e.g. "pubmed". Omit to list all databases.

    Returns:
        Without db: dict with "databases" (list of names). With db: dict with name, description, record_count, last_update, fields (name, full_name, description, term_count) and links (name, description, db_to). On failure, a dict with an "error" key.
    """
    try:
        if db is not None and not db.strip():
            raise ValueError("`db` must be a database name or omitted.")
        result = _entrez_request(Entrez.einfo, db=db.strip() if db else None)
    except Exception as exc:
        return _error("e_info", exc)

    if not db:
        return {"databases": result.get("DbList", [])}

    info = result.get("DbInfo") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    return {
        "name": info.get("DbName"),
        "description": info.get("Description"),
        "record_count": info.get("Count"),
        "last_update": info.get("LastUpdate"),
        "fields": [
            {
                "name": f.get("Name"),
                "full_name": f.get("FullName"),
                "description": f.get("Description"),
                "term_count": f.get("TermCount"),
            }
            for f in info.get("FieldList", [])
            if f.get("IsHidden") != "Y"
        ],
        "links": [
            {
                "name": link.get("Name"),
                "description": link.get("Description"),
                "db_to": link.get("DbTo"),
            }
            for link in info.get("LinkList", [])
        ],
    }


@tool(parse_docstring=True)
def e_post(db: str, ids: List[str], web_env: Optional[str] = None) -> dict:
    """Upload a list of UIDs to the NCBI history server for later batch operations.

    Use this when you have many UIDs (more than ~200) that you want to pass to
    e_fetch, e_summary or e_link: pass the returned query_key and web_env to
    those tools instead of the raw ID list.

    Args:
        db: Entrez database the UIDs belong to, e.g. "pubmed".
        ids: UIDs to upload, e.g. ["31452104", "29669913"].
        web_env: Existing web environment to add this set to, from a previous e_search or e_post. Omit to start a new one.

    Returns:
        dict with query_key, web_env and id_count. On failure, a dict with an "error" key.
    """
    try:
        if not db.strip():
            raise ValueError("`db` must be a non-empty database name.")
        cleaned = _clean_ids(ids)
        if not cleaned:
            raise ValueError("`ids` must contain at least one UID.")
        result = _entrez_request(
            Entrez.epost, db=db.strip(), id=",".join(cleaned), WebEnv=web_env
        )
    except Exception as exc:
        return _error("e_post", exc)

    response = {
        "query_key": result.get("QueryKey"),
        "web_env": result.get("WebEnv"),
        "id_count": len(cleaned),
    }
    if result.get("InvalidIdList"):
        response["invalid_ids"] = result["InvalidIdList"]
    return response


@tool(parse_docstring=True)
def e_summary(
    db: str,
    ids: Optional[List[str]] = None,
    query_key: Optional[str] = None,
    web_env: Optional[str] = None,
    retstart: int = 0,
    retmax: int = 20,
) -> dict:
    """Get document summaries (title, authors, source, dates, etc.) for a set of UIDs.

    Lighter than e_fetch: use this to inspect or rank records before deciding
    which full records to fetch. Provide either ids, or query_key and web_env
    from a prior e_search (with use_history) or e_post call.

    Args:
        db: Entrez database the UIDs belong to, e.g. "pubmed".
        ids: UIDs to summarise (up to 200). Required unless query_key and web_env are given.
        query_key: History-server query key from a prior e_search or e_post call.
        web_env: History-server web environment from a prior e_search or e_post call.
        retstart: Index of the first record to return when using query_key/web_env.
        retmax: Maximum number of summaries to return when using query_key/web_env (1-10000).

    Returns:
        dict with "summaries" (list of summary records, each including its Id) and "count". On failure, a dict with an "error" key.
    """
    try:
        if not db.strip():
            raise ValueError("`db` must be a non-empty database name.")
        cleaned = _clean_ids(ids)
        use_history = bool(query_key and web_env)
        if not cleaned and not use_history:
            raise ValueError("Provide either `ids`, or both `query_key` and `web_env`.")
        if (query_key or web_env) and not use_history:
            raise ValueError("`query_key` and `web_env` must be provided together.")
        if not use_history and len(cleaned) > MAX_IDS_PER_REQUEST:
            raise ValueError(
                f"Too many ids ({len(cleaned)}); use e_post first and pass "
                f"query_key/web_env for more than {MAX_IDS_PER_REQUEST}."
            )
        if retstart < 0:
            raise ValueError("`retstart` must be >= 0.")
        if not 1 <= retmax <= ESUMMARY_MAX_RETMAX:
            raise ValueError(f"`retmax` must be between 1 and {ESUMMARY_MAX_RETMAX}.")

        params = {"db": db.strip()}
        if use_history:
            params.update(query_key=query_key, WebEnv=web_env, retstart=retstart, retmax=retmax)
        else:
            params["id"] = ",".join(cleaned)
        result = _entrez_request(Entrez.esummary, **params)
    except Exception as exc:
        return _error("e_summary", exc)

    # Some databases return a DocumentSummarySet wrapper instead of a flat list
    if isinstance(result, dict):
        result = (
            result.get("DocumentSummarySet", {}).get("DocumentSummary", [])
            or [result]
        )
    return {"summaries": result, "count": len(result)}

@tool(parse_docstring=True)
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

@tool(parse_docstring=True)
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

    Returns:
        With cmd="neighbor": dict with "links", mapping each link name to its
        list of linked UIDs, e.g. {"links": {"pubmed_pmc": ["8123456"]}}.
        With cmd="neighbor_history": dict with "query_key" and "web_env" for
        the linked set, to pass to e_fetch or e_summary.
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

tools = [e_fetch,e_info,e_link,e_post,e_search,e_summary]
tools_by_name = {tool.name: tool for tool in tools}
