import re
import time
import requests
import xml.etree.ElementTree as ET
import streamlit as st

EUTILS_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

def split_pmids(text: str) -> list[str]:
    # comma / spaces / newlines
    pmids = re.split(r"[,\s]+", text.strip())
    pmids = [p for p in pmids if p]
    bad = [p for p in pmids if not p.isdigit()]
    if bad:
        raise ValueError(f"数字以外のPMIDが混ざっています: {', '.join(bad[:20])}" + (" ..." if len(bad) > 20 else ""))
    return pmids

def chunk_list(xs: list[str], n: int) -> list[list[str]]:
    return [xs[i:i+n] for i in range(0, len(xs), n)]

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

def format_authors(author_list: ET.Element | None, max_authors: int) -> str:
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
    if len(authors) > max_authors:
        return ", ".join(authors[:max_authors]) + ", et al."
    return ", ".join(authors)

def format_ama_no_doi_pmid(pubmed_article: ET.Element, max_authors: int, include_issue: bool) -> tuple[str | None, str]:
    pmid = text_or_none(pubmed_article.find("./MedlineCitation/PMID"))

    author_list = pubmed_article.find("./MedlineCitation/Article/AuthorList")
    authors = format_authors(author_list, max_authors=max_authors)

    title = get_all_text(pubmed_article.find("./MedlineCitation/Article/ArticleTitle")) or ""

    journal = (
        text_or_none(pubmed_article.find("./MedlineCitation/Article/Journal/ISOAbbreviation"))
        or text_or_none(pubmed_article.find("./MedlineCitation/MedlineJournalInfo/MedlineTA"))
        or ""
    )

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
        parts.append(f"{authors}.")
    if title:
        parts.append(f"{title}.")
    if journal:
        parts.append(f"{journal}.")

    vol_issue = volume
    if include_issue and issue:
        vol_issue = f"{volume}({issue})" if volume else f"({issue})"

    if year and vol_issue and pages:
        parts.append(f"{year};{vol_issue}:{pages}.")
    elif year and vol_issue:
        parts.append(f"{year};{vol_issue}.")
    elif year:
        parts.append(f"{year}.")

    ref = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return pmid, ref

def reorder_by_input(found: dict[str, str], input_pmids: list[str]) -> list[str]:
    # keep input order; drop missing
    out = []
    for p in input_pmids:
        if p in found:
            out.append(found[p])
    return out

# ---------- UI ----------
st.set_page_config(page_title="PMID → AMA（DOI/PMIDなし）", layout="wide")
st.title("PMID → AMA形式（DOI/PMIDなし）")
st.caption("PMIDを貼り付けてボタンを押すだけ。Excel不要。出力はそのままWordに貼れます。")

with st.sidebar:
    st.header("設定")
    max_authors = st.selectbox("著者表示数（超過は et al.）", [3, 6], index=1)
    include_issue = st.checkbox("Issue (号) を表示する", value=True)

    chunk_size = st.select_slider("1回の取得PMID数（安定優先）", options=[50, 100, 200], value=200)
    rate_sleep = st.select_slider("NCBIアクセス間隔（秒）", options=[0.35, 0.5, 1.0], value=0.35)

    email = st.text_input("NCBI推奨：Email（任意）", value="")
    api_key = st.text_input("NCBI API Key（任意）", value="", type="password")
    tool_name = st.text_input("tool名（任意）", value="pmid_to_ama_text_app")

pmid_text = st.text_area(
    "PMIDを入力（カンマ/改行/スペース区切りOK）",
    height=180,
    placeholder="例：19204236\n7252148\n37112345",
)

col1, col2 = st.columns([1, 2], vertical_alignment="bottom")
with col1:
    do_it = st.button("AMAに変換", type="primary", use_container_width=True)
with col2:
    number_each = st.checkbox("1) 2) 3) … と番号を付ける（任意）", value=False)

if do_it:
    try:
        input_pmids = split_pmids(pmid_text)
        if len(input_pmids) == 0:
            st.warning("PMIDが空です。")
            st.stop()

        found: dict[str, str] = {}
        missing: list[str] = []

        progress = st.progress(0)
        status = st.empty()

        chunks = chunk_list(input_pmids, chunk_size)
        total = len(chunks)

        for i, ch in enumerate(chunks, start=1):
            status.write(f"PubMed取得中… ({i}/{total})")
            xml_text = fetch_pubmed_xml(
                ch,
                tool=tool_name.strip() or "pmid_to_ama_text_app",
                email=email.strip() or None,
                api_key=api_key.strip() or None,
            )
            root = ET.fromstring(xml_text)

            # collect results
            returned_pmids = set()
            for art in root.findall("./PubmedArticle"):
                pmid, ref = format_ama_no_doi_pmid(
                    art, max_authors=max_authors, include_issue=include_issue
                )
                if pmid:
                    returned_pmids.add(pmid)
                    found[pmid] = ref

            for p in ch:
                if p not in returned_pmids:
                    missing.append(p)

            progress.progress(i / total)
            time.sleep(rate_sleep)

        refs = reorder_by_input(found, input_pmids)

        if number_each:
            out_text = "\n".join([f"{idx+1}) {r}" for idx, r in enumerate(refs)])
        else:
            out_text = "\n".join(refs)

        st.subheader("AMA形式（コピーしてWordへ）")
        st.text_area("出力", value=out_text, height=300)

        st.success(f"生成：{len(refs)}件 / 未ヒット：{len(set(missing))}件")

        if missing:
            st.warning("PubMedで見つからなかったPMID（重複除外）")
            st.code("\n".join(sorted(set(missing))), language="text")

    except Exception as e:
        st.error(str(e))
