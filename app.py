import io
import os
import re
from datetime import datetime

import pandas as pd
import pdfplumber
import streamlit as st


COLUMNS = [
    "비엘", "쉬퍼", "쉬퍼주소", "컨사이니", "컨사이니 사업자번호", "컨사이니 주소",
    "선명", "항차", "출발지", "도착지", "품명", "마크", "수량", "중량", "CBM",
    "원본파일명", "확인필요"
]


def group_words_to_lines(words, y_tol=3):
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines = []
    for w in words:
        cy = (w["top"] + w["bottom"]) / 2
        for line in lines:
            if abs(line["cy"] - cy) <= y_tol:
                line["words"].append(w)
                line["cy"] = (line["cy"] * line["n"] + cy) / (line["n"] + 1)
                line["n"] += 1
                break
        else:
            lines.append({"cy": cy, "n": 1, "words": [w]})

    result = []
    for line in sorted(lines, key=lambda x: x["cy"]):
        ws = sorted(line["words"], key=lambda w: w["x0"])
        result.append({
            "top": min(w["top"] for w in ws),
            "bottom": max(w["bottom"] for w in ws),
            "x0": min(w["x0"] for w in ws),
            "x1": max(w["x1"] for w in ws),
            "text": " ".join(w["text"] for w in ws),
            "words": ws,
        })
    return result


def text_from_words(words, y_tol=3):
    return "\n".join(line["text"] for line in group_words_to_lines(words, y_tol=y_tol)).strip()


def words_in_region(page, x0, top, x1, bottom, *, mode="inside"):
    words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
    selected = []
    for w in words:
        if w["top"] < top or w["bottom"] > bottom:
            continue
        if mode == "start":
            ok = x0 <= w["x0"] < x1
        else:
            ok = w["x0"] >= x0 and w["x1"] <= x1
        if ok:
            selected.append(w)
    return selected


def text_in_region(page, x0, top, x1, bottom, *, mode="inside"):
    return text_from_words(words_in_region(page, x0, top, x1, bottom, mode=mode))


def extract_business_no(text):
    """컨사이니명 안의 000-00-00000 형식 사업자번호만 추출합니다."""
    m = re.search(r"(\d{3}-\d{2}-\d{5})", text or "")
    return m.group(1) if m else ""


def clean_company_with_paren(text):
    """컨사이니명 정리.
    - 000-00-00000 형식 사업자번호만 제거
    - 사업자번호 제거 후 남는 빈 괄호 (), （）만 제거
    - 일반 괄호 문구는 임의로 삭제하지 않음
    예: ZZZIP GUESTHOUSE（105-20-88541） -> ZZZIP GUESTHOUSE
        GOGOSS(452-64-00260) -> GOGOSS
        HOMLUX CO., LTD. 563-87-03514 -> HOMLUX CO., LTD.
    """
    text = text or ""
    text = re.sub(r"[\(（]?\s*\b\d{3}-\d{2}-\d{5}\b\s*[\)）]?", " ", text)
    text = re.sub(r"[\(（]\s*[\)）]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    return text


def clean_mark(text):
    lines = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if re.search(r"marks\s*&?\s*nos|container\s*seal", ln, re.I):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def clean_description(text):
    stop_patterns = [
        r"^FREIGHT\b",
        r"^SHIPPED\s+ON\s+BOARD\b",
        r"^Above\s+Particulars\b",
        r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$",
    ]
    lines = []
    seen_said_to_contain = False

    for ln in (text or "").splitlines():
        ln = re.sub(r"\b\d+(?:\.\d+)?\s*KGS\b", "", ln, flags=re.I)
        ln = re.sub(r"\b\d+(?:\.\d+)?\s*CBM\b", "", ln, flags=re.I)
        ln = ln.strip()
        if not ln:
            continue
        if any(re.search(p, ln, re.I) for p in stop_patterns):
            break

        # 품명은 SAID TO CONTAIN 아래부터 시작. 안내 문구 자체는 제외.
        if re.search(r"SHIPPER['’]?S\s+LOAD\s+COUNT", ln, re.I):
            continue
        if re.search(r"SAID\s+TO\s+CONTAIN", ln, re.I):
            seen_said_to_contain = True
            # 같은 줄 뒤에 품명이 붙는 예외가 있으면 뒤쪽만 살림
            tail = re.split(r"SAID\s+TO\s+CONTAIN", ln, flags=re.I)[-1].strip(" :-")
            if tail:
                lines.append(tail)
            continue

        lines.append(ln)

    return "\n".join(lines).strip()


def clean_description_words(words):
    clean = []
    for w in words:
        t = w["text"].strip()
        if re.fullmatch(r"\d+(?:\.\d+)?\s*KGS", t, re.I):
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?\s*CBM", t, re.I):
            continue
        if re.fullmatch(r"\d+\s*PKGS?", t, re.I):
            continue
        clean.append(w)
    return clean_description(text_from_words(clean))


def safe_search(pattern, text, group=1, flags=re.I):
    m = re.search(pattern, text or "", flags)
    return m.group(group).strip() if m else ""


def normalize_port_code(text):
    """Convert common port text to requested customs-style code."""
    raw = (text or "").strip()
    key = re.sub(r"\s+", " ", raw.upper().replace("，", ","))
    key = key.replace(" ", "")
    mapping = {
        "SHIDAOCHINA": "CNSHD",
        "SHIDAO,CHINA": "CNSHD",
        "YANTAICHINA": "CNYNT",
        "YANTAI,CHINA": "CNYNT",
        "WEIHAICHINA": "CNWEI",
        "WEIHAI,CHINA": "CNWEI",
        "GUNSAN,KOREA": "KRKUV",
        "GUNSANKOREA": "KRKUV",
        "INCHEON,KOREA": "KRINC",
        "INCHEONKOREA": "KRINC",
    }
    return mapping.get(key, raw)


def extract_qty_from_mark(mark_text):
    """Fallback quantity recognition from mark ranges, e.g. C/T:1-71, FR-xxx-001 - FR-xxx-037, LJ1-63."""
    txt = (mark_text or "").replace("\n", " ")
    # C/T:1-71, CT: 1 ~ 71
    m = re.search(r"C\s*/?\s*T\s*[:：]?\s*(\d+)\s*[-~]\s*(\d+)", txt, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return str(b - a + 1) if b >= a else str(b)
    # FR-1519594-001 - FR-1519594-037 / GR-...001 - ...005
    nums = re.findall(r"(?:^|[^A-Z0-9])(\d{1,4})(?=\s*(?:$|[^A-Z0-9]))", txt, re.I)
    if len(nums) >= 2:
        # use the last two small sequence numbers if it looks like a range
        a, b = int(nums[-2]), int(nums[-1])
        if 0 <= a <= b <= 9999:
            return str(b - a + 1)
    # LJ1-63, JZF9-1-9: use last number as count when no explicit range exists
    tail = re.search(r"[-\s](\d{1,4})\s*$", txt)
    if tail:
        return str(int(tail.group(1)))
    return ""



def extract_one_pdf(file_bytes, filename, desc_right_ratio=0.89, table_bottom_ratio=0.69):
    warnings = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        if not pdf.pages:
            return {**{c: "" for c in COLUMNS}, "원본파일명": filename, "확인필요": "PDF 페이지 없음"}

        page = pdf.pages[0]
        w, h = page.width, page.height

        bl_region = text_in_region(page, w * 0.58, 0, w, h * 0.18)
        bl = safe_search(r"\b([A-Z0-9]{8,})\b", bl_region)

        shipper_block = text_in_region(page, 0, h * 0.02, w * 0.57, h * 0.10)
        shipper_lines = [x.strip() for x in shipper_block.splitlines() if x.strip()]
        shipper = shipper_lines[0] if shipper_lines else ""
        shipper_addr = "\n".join(shipper_lines[1:]).strip()
        if shipper and not shipper_addr:
            shipper_addr = "CHINA"

        consignee_block = text_in_region(page, 0, h * 0.09, w * 0.57, h * 0.16)
        consignee_lines = [x.strip() for x in consignee_block.splitlines() if x.strip()]
        consignee_raw = consignee_lines[0] if consignee_lines else ""
        consignee_no = extract_business_no(consignee_raw)
        consignee = clean_company_with_paren(consignee_raw)
        consignee_addr = "\n".join(consignee_lines[1:])

        notify_block = text_in_region(page, 0, h * 0.17, w * 0.57, h * 0.29)

        vessel_region = text_in_region(page, 0, h * 0.31, w * 0.25, h * 0.37).replace("\n", " ")
        vessel = ""
        voyage = ""
        m = re.search(r"([A-Z]+(?:\s+[A-Z0-9]+)*)\s+([0-9A-Z]+)$", vessel_region)
        if m:
            vessel, voyage = m.group(1).strip(), m.group(2).strip()
        else:
            vessel = vessel_region.strip()
        # 항차가 1538처럼 숫자만 있는 경우 E를 붙임: 1538 -> 1538E
        if re.fullmatch(r"\d+", voyage or ""):
            voyage = f"{voyage}E"

        pol_raw = text_in_region(page, w * 0.25, h * 0.31, w * 0.48, h * 0.37).replace("\n", " ").strip()
        pod_raw = text_in_region(page, 0, h * 0.35, w * 0.28, h * 0.40).replace("\n", " ").strip()
        pol = normalize_port_code(pol_raw)
        pod = normalize_port_code(pod_raw)

        table_top = h * 0.39
        table_bottom = h * table_bottom_ratio

        mark_words = words_in_region(page, 0, table_top, w * 0.28, table_bottom, mode="start")
        mark = clean_mark(text_from_words(mark_words))

        pkg_region = text_in_region(page, w * 0.27, table_top, w * 0.39, table_top + h * 0.08, mode="start")
        pkg = safe_search(r"(\d+)\s*PKGS?", pkg_region)
        if not pkg:
            pkg = extract_qty_from_mark(mark)

        desc_left = w * 0.37
        desc_right = w * desc_right_ratio
        desc_words = words_in_region(page, desc_left, table_top, desc_right, table_bottom, mode="start")
        description = clean_description_words(desc_words)

        weight_region = text_in_region(page, w * 0.76, table_top, w * 0.90, table_top + h * 0.08, mode="start").replace("\n", " ")
        weight = safe_search(r"(\d+(?:\.\d+)?)\s*KGS", weight_region)

        cbm_region = text_in_region(page, w * 0.88, table_top, w, table_top + h * 0.08, mode="start").replace("\n", " ")
        cbm = safe_search(r"(\d+(?:\.\d+)?)\s*CBM", cbm_region)

        if consignee_raw and "(" in consignee_raw and consignee.endswith("()"):
            consignee = consignee.replace("()", "").strip()
        if not mark:
            warnings.append("마크 미인식")
        if not description:
            warnings.append("품명 미인식")
        if not weight:
            warnings.append("중량 확인필요")
        if not cbm:
            warnings.append("CBM 확인필요")
        if not pkg:
            warnings.append("수량 확인필요")

        return {
            "비엘": bl,
            "쉬퍼": shipper,
            "쉬퍼주소": shipper_addr,
            "컨사이니": consignee,
            "컨사이니 사업자번호": consignee_no,
            "컨사이니 주소": consignee_addr,
            "선명": vessel,
            "항차": voyage,
            "출발지": pol,
            "도착지": pod,
            "품명": description,
            "마크": mark,
            "수량": pkg,
            "중량": weight,
            "CBM": cbm,
            "원본파일명": filename,
            "확인필요": ", ".join(warnings),
        }


def make_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="변환결과")
        ws = writer.book["변환결과"]
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = cell.font.copy(bold=True)
            cell.alignment = cell.alignment.copy(horizontal="center", vertical="center")
        widths = {
            "A": 22, "B": 30, "C": 42, "D": 24, "E": 20, "F": 45,
            "G": 20, "H": 12, "I": 18, "J": 18, "K": 55, "L": 35,
            "M": 10, "N": 12, "O": 10, "P": 34, "Q": 28,
        }
        for col, width in widths.items():
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = cell.alignment.copy(wrap_text=True, vertical="top")
        for r in range(2, ws.max_row + 1):
            ws.row_dimensions[r].height = 95
    output.seek(0)
    return output.getvalue()


st.set_page_config(page_title="BL PDF 변환기", page_icon="📄", layout="wide")

st.title("📄 BL PDF → Excel 변환기")
st.caption("v23 기준 수정본: 빈 괄호 제거 / 품명은 SAID TO CONTAIN 아래부터")

with st.sidebar:
    st.subheader("추출 설정")
    desc_mode = st.radio(
        "품명 오른쪽 범위",
        ["안전모드", "넓게 인식"],
        help="안전모드는 중량칸 혼입을 더 강하게 막고, 넓게 인식은 길게 넘어간 품명을 더 많이 잡습니다.",
    )
    desc_right_ratio = 0.80 if desc_mode == "안전모드" else 0.89
    table_bottom_ratio = st.slider(
        "MARK/품명 하단 범위",
        min_value=0.55,
        max_value=0.78,
        value=0.69,
        step=0.01,
        help="마크가 아래로 길게 이어지는 특수 PDF면 조금 올려주세요. 너무 올리면 하단 문구가 포함될 수 있습니다.",
    )

uploaded = st.file_uploader("BL PDF 파일 업로드", type=["pdf"], accept_multiple_files=True)

if uploaded:
    rows = []
    progress = st.progress(0)
    for idx, file in enumerate(uploaded, start=1):
        try:
            rows.append(extract_one_pdf(file.getvalue(), file.name, desc_right_ratio, table_bottom_ratio))
        except Exception as e:
            rows.append({**{c: "" for c in COLUMNS}, "원본파일명": file.name, "확인필요": f"오류: {e}"})
        progress.progress(idx / len(uploaded))

    df = pd.DataFrame(rows, columns=COLUMNS)

    st.subheader("미리보기")
    st.dataframe(df, use_container_width=True, hide_index=True)

    excel_bytes = make_excel(df)
    file_name = f"BL_PDF_변환결과_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    st.download_button(
        "엑셀 다운로드",
        data=excel_bytes,
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    with st.expander("이번 수정 반영 내용"):
        st.write("- 컨사이니명에서 `(사업자번호)` 제거 후 사업자번호는 별도 칸에 저장")
        st.write("- MARK는 줄 수 기준이 아니라 왼쪽 MARK 구역에 있는 텍스트 전체를 추출")
        st.write("- 품명은 Description 구역 기준으로 추출하고, KGS/CBM/PKGS 패턴은 품명에서 자동 제외")
        st.write("- 수량은 PKGS 칸이 비어도 MARK 범위(C/T:1-71, FR-001~037 등)에서 자동 계산")
        st.write("- 출발지/도착지는 SHIDAO CHINA=CNSHD, YANTAI CHINA=CNYNT, WEIHAI CHINA=CNWEI, GUNSAN KOREA=KRKUV, INCHEON KOREA=KRINC로 변환")
        st.write("- 항차가 숫자만 있으면 뒤에 E 자동 추가 예: 1538 → 1538E")
        st.write("- 쉬퍼가 있는데 쉬퍼주소가 비어 있으면 CHINA 자동 입력")
else:
    st.info("PDF를 업로드하면 자동으로 엑셀 변환 결과가 생성됩니다.")
