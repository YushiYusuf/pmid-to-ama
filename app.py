import re
import time
import requests
import xml.etree.ElementTree as ET
import streamlit as st

EUTILS_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EUTILS_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

# ===== Fixed operational settings (hidden from UI) =====
CHUNK_SIZE = 200          # number of PMIDs per EFetch
RATE_SLEEP = 0.5          # seconds between NCBI calls (throttle)
TOOL_NAME = "pmid_doi_to_ama_text_app"
EMAIL = None              # set to "your_email@example.com" if you want
API_KEY = None            # set to your NCBI API key if you want
# =======================================================

# ---------- Input parsing ----------
def split_tokens(text: str) -> list[str]:
    tokens = re.split(r"[,\s]+", text.strip())
    return [t for t in tokens if t]

def is_pmid(t: str) -> bool:
    return t.isdigit()

def is_doi(t: str) -> bool:
    t = t.strip()
    if " " in t:
        return False
    if t.lower().startswith("doi:"):
        t = t[4:].strip()
    return (t.startswith("10.") and "/" in t) or ("/" in t and "10." in t)

def normalise_doi(t: str) -> str:
    t = t.strip()
    if t.lower().startswith("doi:"):
        t = t[4:].strip()
    t = re.sub(r"^https?://(dx\.)?doi\.org/", "", t, flags=re.IGNORECASE)
    return t

def chunk_list(xs: list[str], n: int) -> list[list[str]]:
    return [xs[i:i+n] for i in range(0, len(xs), n)]

# ---------- PubMed calls ----------
def esearch_pmid_from_doi(doi: str) -> str | None:
    params = {
        "db": "pubmed",
        "retmode": "xml",
        "tool": TOOL_NAME,
        "term": f"\"{doi}\"[AID] OR \"{doi}\"[doi]",
    }
    if EMAIL:
        params["email"] = EMAIL
    if API_KEY:
        params["api_key"] = API_KEY

    r = requests.get(EUTILS_ESEARCH, params=params, timeout=45)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    id_el = root.find(".//IdList/Id")
    return id_el.text.strip() if id_el is not None and (id_el.text or "").strip() else None

def fetch_pubmed_xml(pmids: list[str]) -> str:
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": TOOL_NAME,
    }
    if EMAIL:
        params["email"] = EMAIL
    if API_KEY:
        params["api_key"] = API_KEY

    r = requests.get(EUTILS_EFETCH, params=params, timeout=45)
    r.raise_for_status()
    return r.text

# ---------- XML helpers ----------
def text_or_none(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    t = (elem.text or "").strip()
    return t if t else None

def get_all_text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    return "".join(elem.itertext()).strip()

# ---------- Formatting helpers ----------
def ensure_one_period(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[.\s]+$", "", s)
    return f"{s}." if s else ""

def format_authors(author_list: ET.Element | None, mode: str) -> str:
    # mode: "all" or "3etal"
    if author_list is None:
        return ""
    authors = []
    for a in author_list.findall("./Author"):
        collective = text_or_none(a.find("./CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        last = text_or_none(a.find("./LastName"))
        initials = text_or_none(a.find("./Initials"))
        if last and initials:
            authors.append(f"{last} {initials}")
        elif last:
            authors.append(last)

    if not authors:
        return ""

    if mode == "3etal" and len(authors) > 3:
        return ", ".join(authors[:3]) + ", et al."
    return ", ".join(authors)

def format_ama(pubmed_article: ET.Element, author_mode: str, include_issue: bool) -> tuple[str | None, str]:
    pmid = text_or_none(pubmed_article.find("./MedlineCitation/PMID"))

    author_list = pubmed_article.find("./MedlineCitation/Article/AuthorList")
    authors = format_authors(author_list, mode=author_mode)

    title_raw = get_all_text(pubmed_article.find("./MedlineCitation/Article/ArticleTitle")) or ""
    title = ensure_one_period(title_raw)

    journal_raw = (
        text_or_none(pubmed_article.find("./MedlineCitation/Article/Journal/ISOAbbreviation"))
        or text_or_none(pubmed_article.find("./MedlineCitation/MedlineJournalInfo/MedlineTA"))
        or ""
    )
    journal = ensure_one_period(journal_raw)

    year = (
        text_or_none(pubmed_article.find("./MedlineCitation/Article/Journal/JournalIssue/PubDate/Year"))
        or text_or_none(pubmed_article.find("./MedlineCitation/Article/ArticleDate/Year"))
        or ""
    )

    volume = text_or_none(pubmed_article.find("./MedlineCitation/Article/Journal/JournalIssue/Volume")) or ""
    issue = text_or_none(pubmed_article.find("./MedlineCitation/Article/Journal/JournalIssue/Issue")) or ""
    pages = text_or_none(pubmed_article.find("./MedlineCitation/Article/Pagination/MedlinePgn")) or ""

    parts = []
    if authors:
        parts.append(ensure_one_period(authors))
    if title:
        parts.append(title)
    if journal:
        parts.append(journal)

    vol_issue = volume
    if include_issue and issue:
        vol_issue = f"{volume}({issue})" if volume else f"({issue})"

    if year and vol_issue and pages:
        parts.append(f"{year};{vol_issue}:{pages}.")
    elif year and vol_issue:
        parts.append(f"{year};{vol_issue}.")
    elif year:
        parts.append(f"{year}.")

    ref = " ".join([p.strip() for p in parts if p.strip()])
    ref = re.sub(r"\s+", " ", ref).strip()
    ref = re.sub(r"\.\.+", ".", ref)
    return pmid, ref

def reorder_by_input(found: dict[str, str], pmids_in_order: list[str]) -> list[str]:
    return [found[p] for p in pmids_in_order if p in found]

# ---------- UI ----------
st.set_page_config(page_title="PMID/DOI → AMA（テキスト）", layout="wide")
st.title("PMID / DOI → AMA形式（テキスト出力）")
st.caption("PMIDでもDOIでも貼り付けOK。出力はAMA形式テキスト。")

author_mode_label = st.radio("著者表示", ["全員表示", "3名まで + et al."], index=1, horizontal=True)
author_mode = "all" if author_mode_label == "全員表示" else "3etal"

include_issue = st.checkbox("Issue (号) を表示する", value=True)
number_each = st.checkbox("1) 2) 3) … と番号を付ける", value=False)

input_text = st.text_area(
    "PMID または DOI を入力（カンマ/改行/スペース区切りで複数OK）",
    height=180,
    placeholder="例：\n32096709\ndoi:10.1148/radiol.2020191710\nhttps://doi.org/10.1148/radiol.2020191710",
)

if st.button("AMAに変換", type="primary", use_container_width=True):
    try:
        tokens = split_tokens(input_text)
        if not tokens:
            st.warning("入力が空です。")
            st.stop()

        resolved_pmids_in_order: list[str] = []
        invalid_tokens: list[str] = []
        doi_not_found: list[str] = []

        status = st.empty()
        progress = st.progress(0.0)

        # 1) Resolve PMID/DOI -> PMIDs
        total_resolve = len(tokens)
        for i, t in enumerate(tokens, start=1):
            tt = t.strip()
            if is_pmid(tt):
                resolved_pmids_in_order.append(tt)
            elif is_doi(tt):
                doi = normalise_doi(tt)
                status.write(f"DOI→PMID 変換中… ({i}/{total_resolve})")
                pmid = esearch_pmid_from_doi(doi)
                if pmid:
                    resolved_pmids_in_order.append(pmid)
                else:
                    doi_not_found.append(doi)
                time.sleep(RATE_SLEEP)
            else:
                invalid_tokens.append(tt)

            progress.progress(i / total_resolve)

        if invalid_tokens:
            st.warning("PMIDでもDOIでもない入力（無視されます）")
            st.code("\n".join(invalid_tokens), language="text")

        if doi_not_found:
            st.warning("PubMedでPMIDに解決できなかったDOI")
            st.code("\n".join(sorted(set(doi_not_found))), language="text")

        # Deduplicate while preserving order
        seen = set()
        resolved_pmids_in_order = [p for p in resolved_pmids_in_order if not (p in seen or seen.add(p))]

        if not resolved_pmids_in_order:
            st.error("有効なPMID/DOIがありませんでした。")
            st.stop()

        # 2) Fetch details
        found: dict[str, str] = {}
        missing_pmids: list[str] = []

        chunks = chunk_list(resolved_pmids_in_order, CHUNK_SIZE)
        total_chunks = len(chunks)

        progress = st.progress(0.0)
        for ci, ch in enumerate(chunks, start=1):
            status.write(f"PubMed取得中… ({ci}/{total_chunks})")
            xml_text = fetch_pubmed_xml(ch)
            root = ET.fromstring(xml_text)

            returned = set()
            for art in root.findall("./PubmedArticle"):
                pmid, ref = format_ama(art, author_mode=author_mode, include_issue=include_issue)
                if pmid:
                    returned.add(pmid)
                    found[pmid] = ref

            for p in ch:
                if p not in returned:
                    missing_pmids.append(p)

            progress.progress(ci / total_chunks)
            time.sleep(RATE_SLEEP)

        refs = reorder_by_input(found, resolved_pmids_in_order)

        out_text = "\n".join([f"{i+1}) {r}" for i, r in enumerate(refs)]) if number_each else "\n".join(refs)

        st.subheader("AMA形式（コピーしてWordへ）")
        st.text_area("出力", value=out_text, height=320)

        st.success(f"生成：{len(refs)}件 / 未ヒット：{len(set(missing_pmids))}件")

        if missing_pmids:
            st.warning("PubMedで見つからなかったPMID（重複除外）")
            st.code("\n".join(sorted(set(missing_pmids))), language="text")

    except Exception as e:
        st.error(str(e))
