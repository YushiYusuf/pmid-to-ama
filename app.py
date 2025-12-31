import re
import time
import requests
import xml.etree.ElementTree as ET
import streamlit as st

EUTILS_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EUTILS_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

# ---------- Input parsing ----------
def split_tokens(text: str) -> list[str]:
    tokens = re.split(r"[,\s]+", text.strip())
    return [t for t in tokens if t]

def is_pmid(t: str) -> bool:
    return t.isdigit()

def is_doi(t: str) -> bool:
    # relaxed DOI detection: contains "/" and no spaces, typically starts with "10."
    t = t.strip()
    if " " in t:
        return False
    # allow "doi:10.xxxx/yyy"
    if t.lower().startswith("doi:"):
        t = t[4:]
    return (t.startswith("10.") and "/" in t) or ("/" in t and "10." in t)

def normalise_doi(t: str) -> str:
    t = t.strip()
    if t.lower().startswith("doi:"):
        t = t[4:].strip()
    # remove URL prefix if pasted
    t = re.sub(r"^https?://(dx\.)?doi\.org/", "", t, flags=re.IGNORECASE)
    return t

# ---------- PubMed calls ----------
def esearch_pmid_from_doi(doi: str, tool: str, email: str | None, api_key: str | None) -> str | None:
    """
    Resolve DOI -> PMID via PubMed ESearch.
    """
    params = {
        "db": "pubmed",
        "retmode": "xml",
        "tool": tool,
        # AID (Article Identifier) covers DOI in PubMed; also try [doi]
        "term": f"\"{doi}\"[AID] OR \"{doi}\"[doi]"
    }
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key

    r = requests.get(EUTILS_ESEARCH, params=params, timeout=45)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    id_el = root.find(".//IdList/Id")
    return id_el.text.strip() if id_el is not None and (id_el.text or "").strip() else None

def fetch_pubmed_xml(pmids: list[str], tool: str, email: str | None, api_key: str | None) -> str:
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": tool,
    }
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key

    r = requests.get(EUTILS_EFETCH, params=params, timeout=45)
    r.raise_for_status()
    return r.text

def chunk_list(xs: list[str], n: int) -> list[list[str]]:
    return [xs[i:i+n] for i in range(0, len(xs), n)]

# ---------- XML helpers ----------
def text_or_none(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    t = (elem.text or "").strip()
    return t if t else None

def get_all_text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    t = "".join(elem.itertext()).strip()
    return t if t else None

# ---------- Formatting helpers ----------
def ensure_one_period(s: str) -> str:
    """
    Ensure the string ends with exactly one period.
    Remove trailing periods/spaces then add one.
    """
    s = (s or "").strip()
    s = re.sub(r"[.\s]+$", "", s)
    return f"{s}." if s else ""

def format_authors(author_list: ET.Element | None, mode: str) -> str:
    """
    mode: 'all' or '3etal'
    """
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

    # Title sometimes already ends with '.', which caused ".."
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

    # Build
    parts = []
    if authors:
        parts.append(ensure_one_period(authors))  # ensures single trailing period
    if title:
        parts.append(title)                       # already ends with single period
    if journal:
        parts.append(journal)                     # already ends with single period

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
    # final cleanup: avoid accidental ".."
    ref = re.sub(r"\.\.+", ".", ref)
    return pmid, ref

def reorder_by_input(found: dict[str, str], input_pmids_in_order: list[str]) -> list[str]:
    out = []
    for p in input_pmids_in_order:
        if p in found:
            out.append(found[p])
    return out

# ---------- UI ----------
st.set_page_config(page_title="PMID/DOI → AMA（テキスト）", layout="wide")
st.title("PMID / DOI → AMA形式（テキスト出力）")
st.caption("PMIDでもDOIでも貼り付けOK。出力はAMA形式テキスト。")

with st.sidebar:
    st.header("設定")
    author_mode_label = st.radio(
        "著者表示",
        ["全員表示", "3名まで + et al."],
        index=1
    )
    author_mode = "all" if author_mode_label == "全員表示" else "3etal"

    include_issue = st.checkbox("Issue (号) を表示する", value=True)
    number_each = st.checkbox("1) 2) 3) … と番号を付ける", value=False)

    chunk_size = st.select_slider("1回の取得件数（安定優先）", options=[50, 100, 200], value=200)
    rate_sleep = st.select_slider("NCBIアクセス間隔（秒）", options=[0.35, 0.5, 1.0], value=0.35)

    email = st.text_input("NCBI推奨：Email（任意）", value="")
    api_key = st.text_input("NCBI API Key（任意）", value="", type="password")
    tool_name = st.text_input("tool名（任意）", value="pmid_doi_to_ama_text_app")

st.markdown("### 入力（PMID または DOI を貼り付け）")
input_text = st.text_area(
    "カンマ/改行/スペース区切りで複数OK（doi: 付きや https://doi.org/ もOK）",
    height=180,
    placeholder="例：\n19204236\ndoi:10.1001/jama.2020.1585\nhttps://doi.org/10.1056/NEJMoa2034577"
)

if st.button("AMAに変換", type="primary", use_container_width=True):
    try:
        tokens = split_tokens(input_text)
        if not tokens:
            st.warning("入力が空です。")
            st.stop()

        # Resolve tokens -> PMIDs in input order
        resolved_pmids_in_order: list[str] = []
        invalid_tokens: list[str] = []
        doi_not_found: list[str] = []

        status = st.empty()
        progress = st.progress(0.0)

        # 1) Resolve DOIs to PMIDs (and keep PMIDs)
        total_resolve = len(tokens)
        for i, t in enumerate(tokens, start=1):
            tt = t.strip()
            if is_pmid(tt):
                resolved_pmids_in_order.append(tt)
            elif is_doi(tt):
                doi = normalise_doi(tt)
                status.write(f"DOI→PMID 変換中… ({i}/{total_resolve})")
                pmid = esearch_pmid_from_doi(
                    doi=doi,
                    tool=tool_name.strip() or "pmid_doi_to_ama_text_app",
                    email=email.strip() or None,
                    api_key=api_key.strip() or None,
                )
                if pmid:
                    resolved_pmids_in_order.append(pmid)
                else:
                    doi_not_found.append(doi)
                time.sleep(rate_sleep)
            else:
                invalid_tokens.append(tt)

            progress.progress(i / total_resolve)

        if invalid_tokens:
            st.warning("PMIDでもDOIでもない入力が含まれています（無視されます）")
            st.code("\n".join(invalid_tokens), language="text")

        if doi_not_found:
            st.warning("PubMedでPMIDに解決できなかったDOI")
            st.code("\n".join(sorted(set(doi_not_found))), language="text")

        # De-duplicate while preserving order (optional but helpful)
        seen = set()
        resolved_pmids_in_order = [p for p in resolved_pmids_in_order if not (p in seen or seen.add(p))]

        if not resolved_pmids_in_order:
            st.error("有効なPMID/DOIがありませんでした。")
            st.stop()

        # 2) Fetch PubMed details in chunks
        found: dict[str, str] = {}
        missing_pmids: list[str] = []

        chunks = chunk_list(resolved_pmids_in_order, chunk_size)
        total_chunks = len(chunks)

        progress = st.progress(0.0)
        for ci, ch in enumerate(chunks, start=1):
            status.write(f"PubMed取得中… ({ci}/{total_chunks})")
            xml_text = fetch_pubmed_xml(
                pmids=ch,
                tool=tool_name.strip() or "pmid_doi_to_ama_text_app",
                email=email.strip() or None,
                api_key=api_key.strip() or None,
            )
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
            time.sleep(rate_sleep)

        refs = reorder_by_input(found, resolved_pmids_in_order)

        # Output text
        if number_each:
            out_text = "\n".join([f"{i+1}) {r}" for i, r in enumerate(refs)])
        else:
            out_text = "\n".join(refs)

        st.subheader("AMA形式")
        st.text_area("出力", value=out_text, height=320)

        st.success(f"生成：{len(refs)}件 / 未ヒット：{len(set(missing_pmids))}件")

        if missing_pmids:
            st.warning("PubMedで見つからなかったPMID（重複除外）")
            st.code("\n".join(sorted(set(missing_pmids))), language="text")

    except Exception as e:
        st.error(str(e))
